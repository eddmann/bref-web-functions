use anyhow::{bail, Context, Result};
use bref_web_gateway::{config::Config, connection, gateway::Gateway};
use nix::{
    sys::signal::{killpg, Signal},
    unistd::Pid,
};
use std::{
    future::pending, os::unix::process::CommandExt, process::Stdio, sync::Arc, time::Duration,
};
use tokio::{
    net::{TcpListener, TcpStream},
    process::{Child, Command},
    sync::watch,
    task::JoinSet,
    time::{sleep, timeout, Instant},
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let external = std::env::args()
        .skip(1)
        .any(|argument| argument == "--external-fpm");
    let config = Config::from_env()?;
    // Register before spawning children so a stop during readiness cleans up too.
    let term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let mut child = if external {
        None
    } else {
        let mut command = Command::new(&config.fpm_bin);
        command.as_std_mut().process_group(0);
        Some(
            command
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
                .kill_on_drop(true)
                .spawn()
                .context("starting PHP-FPM master")?,
        )
    };
    let group_id = child
        .as_ref()
        .and_then(Child::id)
        .map(|id| Pid::from_raw(id as i32));
    let _group_guard = ProcessGroupGuard(group_id);
    let result = serve(&config, &mut child, group_id, term, interrupt).await;
    // Readiness/bind/accept failures must also clean up all FPM children.
    stop_fpm(&mut child, group_id, config.shutdown_grace).await;
    result
}

struct ProcessGroupGuard(Option<Pid>);
impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        if let Some(group) = self.0 {
            let _ = killpg(group, Signal::SIGKILL);
        }
    }
}

async fn serve(
    config: &Config,
    child: &mut Option<Child>,
    group_id: Option<Pid>,
    term: tokio::signal::unix::Signal,
    interrupt: tokio::signal::unix::Signal,
) -> Result<()> {
    let signal = shutdown_signal(term, interrupt);
    tokio::pin!(signal);
    tokio::select! {
        result = wait_for_fpm(&config.fpm_addr, child) => result?,
        _ = &mut signal => return Ok(()),
    }
    let listener = TcpListener::bind(&config.endpoint)
        .await
        .context("binding AWS HTTP endpoint")?;
    eprintln!(
        "bref-web Rust prototype ready endpoint={} fpm={} max_in_flight={}",
        config.endpoint, config.fpm_addr, config.max_in_flight
    );
    let gateway = Arc::new(Gateway::new(config.clone()));
    let mut retirement = gateway.retirement();
    let (shutdown_sender, shutdown_receiver) = watch::channel(false);
    let mut connections = JoinSet::new();
    let mut master_failure = None;
    loop {
        tokio::select! {
            _ = &mut signal => { break; }
            _ = retirement.changed() => {
                master_failure = Some(format!(
                    "runtime retiring: {}",
                    retirement.borrow().unwrap_or("retirement notification closed")
                ));
                break;
            }
            status = async { match child { Some(child) => child.wait().await, None => pending().await } } => {
                master_failure = Some(format!("PHP-FPM master exited unexpectedly: {status:?}"));
                break;
            }
            connection = listener.accept() => {
                let (stream, _) = connection.context("accepting HTTP connection")?;
                let service_gateway = gateway.clone();
                let shutdown = shutdown_receiver.clone();
                connections.spawn(async move {
                    if let Err(error) = connection::serve(stream, service_gateway, shutdown).await {
                        eprintln!("bref-web http_connection_error={error}");
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
        if let Some(master) = child {
            if let Some(status) = master.try_wait()? {
                bail!("PHP-FPM exited before readiness: {status}");
            }
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

async fn stop_fpm(child: &mut Option<Child>, group_id: Option<Pid>, grace: Duration) {
    if let Some(mut master) = child.take() {
        if let Some(group) = group_id {
            let _ = killpg(group, Signal::SIGQUIT);
        }
        if timeout(grace, master.wait()).await.is_err() {
            if let Some(group) = group_id {
                let _ = killpg(group, Signal::SIGKILL);
            }
            let _ = master.start_kill();
            let _ = master.wait().await;
        }
        if let Some(group) = group_id {
            let _ = killpg(group, Signal::SIGKILL);
        }
    }
}

async fn shutdown_signal(
    mut term: tokio::signal::unix::Signal,
    mut interrupt: tokio::signal::unix::Signal,
) {
    tokio::select! { _ = term.recv() => {}, _ = interrupt.recv() => {} }
}
