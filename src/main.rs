use anyhow::{bail, Context, Result};
use bref_web_gateway::{
    config::Config,
    connection::{self, AcceptFailure},
    gateway::Gateway,
};
use nix::{
    sys::signal::{killpg, Signal},
    unistd::Pid,
};
use std::{os::unix::process::CommandExt, pin::Pin, process::Stdio, sync::Arc, time::Duration};
use tokio::{
    net::{TcpListener, TcpStream},
    process::{Child, Command},
    sync::watch,
    task::JoinSet,
    time::{sleep, timeout, Instant, Sleep},
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let started = Instant::now();
    let config = Config::from_env()?;
    // Register before spawning children so a stop during readiness cleans up too.
    let term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    // The FPM master leads its own process group, so stopping the group also
    // stops workers the master no longer tracks.
    let mut command = Command::new(&config.fpm_bin);
    command.as_std_mut().process_group(0);
    let master = command
        .args([
            "-F",
            "-O",
            "-y",
            &config.fpm_config,
            "-d",
            "opcache.file_cache_only=0",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()
        .context("starting PHP-FPM master")?;
    let group_id = Pid::from_raw(master.id().context("PHP-FPM has no pid")? as i32);
    let _group_guard = ProcessGroupGuard(group_id);
    // `None` once FPM has been stopped.
    let mut child = Some(master);
    let result = serve(&config, &mut child, group_id, term, interrupt, started).await;
    // Readiness/bind/accept failures must also clean up all FPM children.
    stop_fpm(&mut child, group_id, config.shutdown_grace).await;
    result
}

/// Kills the FPM process group on every exit path, including panics.
struct ProcessGroupGuard(Pid);
impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        let _ = killpg(self.0, Signal::SIGKILL);
    }
}

async fn serve(
    config: &Config,
    child: &mut Option<Child>,
    group_id: Pid,
    term: tokio::signal::unix::Signal,
    interrupt: tokio::signal::unix::Signal,
    started: Instant,
) -> Result<()> {
    let signal = shutdown_signal(term, interrupt);
    tokio::pin!(signal);
    tokio::select! {
        result = wait_for_fpm(&config.fpm_addr, child) => result?,
        _ = &mut signal => return Ok(()),
    }
    let fpm_ready_ms = started.elapsed().as_millis();
    let listener = TcpListener::bind(&config.endpoint)
        .await
        .context("binding AWS HTTP endpoint")?;
    bref_web_gateway::log_line!(
        "bref-web ready endpoint={} fpm={} max_in_flight={} fpm_ready_ms={} startup_ms={}",
        config.endpoint,
        config.fpm_addr,
        config.max_in_flight,
        fpm_ready_ms,
        started.elapsed().as_millis()
    );
    let gateway = Arc::new(Gateway::new(config.clone()));
    let mut retirement = gateway.retirement();
    let (shutdown_sender, shutdown_receiver) = watch::channel(false);
    let mut connections = JoinSet::new();
    let mut master_failure = None;
    // Set while accepting is paused after resource exhaustion. Signals,
    // retirement and FPM exit are still handled during the pause.
    let mut accept_paused: Option<Pin<Box<Sleep>>> = None;
    loop {
        tokio::select! {
            _ = &mut signal => { break; }
            _ = retirement.changed() => {
                master_failure = Some(format!(
                    "runtime retiring: {}",
                    retirement.borrow().as_deref().unwrap_or("retirement notification closed")
                ));
                break;
            }
            status = async { child.as_mut().expect("FPM runs while serving").wait().await } => {
                master_failure = Some(format!("PHP-FPM master exited unexpectedly: {status:?}"));
                break;
            }
            _ = async { if let Some(pause) = accept_paused.as_mut() { pause.await } }, if accept_paused.is_some() => {
                accept_paused = None;
            }
            connection = listener.accept(), if accept_paused.is_none() => {
                let stream = match connection {
                    Ok((stream, _)) => stream,
                    Err(error) => {
                        match connection::accept_failure(&error) {
                            AcceptFailure::Retry => bref_web_gateway::log_line!("bref-web accept_error={error} action=retry"),
                            AcceptFailure::Pause => {
                                bref_web_gateway::log_line!("bref-web accept_error={error} action=pause pause_ms={}", connection::ACCEPT_PAUSE.as_millis());
                                accept_paused = Some(Box::pin(sleep(connection::ACCEPT_PAUSE)));
                            }
                            AcceptFailure::Fatal => return Err(error).context("accepting HTTP connection"),
                        }
                        continue;
                    }
                };
                let service_gateway = gateway.clone();
                let shutdown = shutdown_receiver.clone();
                connections.spawn(async move {
                    if let Err(error) = connection::serve(stream, service_gateway, shutdown).await {
                        bref_web_gateway::log_line!("bref-web {error}");
                    }
                });
            }
            _ = connections.join_next(), if !connections.is_empty() => {}
        }
    }
    drop(listener);
    gateway.close_admission();
    let _ = shutdown_sender.send(true);
    let shutdown_deadline = Instant::now() + config.shutdown_grace;
    gateway.drain(config.shutdown_grace).await;
    stop_fpm(
        child,
        group_id,
        shutdown_deadline.saturating_duration_since(Instant::now()),
    )
    .await;
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    if let Some(error) = master_failure {
        bail!(error);
    }
    Ok(())
}

async fn wait_for_fpm(addr: &str, child: &mut Option<Child>) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.as_mut().expect("FPM runs until stopped").try_wait()? {
            bail!("PHP-FPM exited before readiness: {status}");
        }
        if TcpStream::connect(addr).await.is_ok() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            bail!("PHP-FPM readiness timed out at {addr}");
        }
        sleep(Duration::from_millis(25)).await;
    }
}

/// SIGQUIT (graceful), then SIGKILL after `grace`; always kills the whole group.
async fn stop_fpm(child: &mut Option<Child>, group: Pid, grace: Duration) {
    let Some(mut master) = child.take() else {
        return;
    };
    let _ = killpg(group, Signal::SIGQUIT);
    if timeout(grace, master.wait()).await.is_err() {
        let _ = killpg(group, Signal::SIGKILL);
        let _ = master.wait().await;
    }
    let _ = killpg(group, Signal::SIGKILL);
}

async fn shutdown_signal(
    mut term: tokio::signal::unix::Signal,
    mut interrupt: tokio::signal::unix::Signal,
) {
    tokio::select! { _ = term.recv() => {}, _ = interrupt.recv() => {} }
}
