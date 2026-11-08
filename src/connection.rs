//! Drive HTTP/1 connections with a deadline independent of response-body polling.
use crate::gateway::Gateway;
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::rt::TokioIo;
use std::{future::pending, io, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::watch,
    time::{sleep_until, Instant},
};

// Let a handler flush its terminal 408/504 response at the HTTP deadline. A
// blocked transport still has a finite cutoff, including after the last body
// frame was polled. This grace does not extend the PHP request deadline.
pub const RESPONSE_FLUSH_GRACE: Duration = Duration::from_millis(100);

pub async fn serve<S>(
    stream: S,
    gateway: Arc<Gateway>,
    mut shutdown: watch::Receiver<bool>,
) -> io::Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (deadline_sender, mut deadlines) = watch::channel::<Option<Instant>>(None);
    let service = service_fn(move |request| {
        let ingress = Instant::now();
        deadline_sender.send_replace(Some(
            ingress + gateway.config.request_timeout + RESPONSE_FLUSH_GRACE,
        ));
        gateway.clone().handle_at(request, ingress)
    });
    let connection = http1::Builder::new()
        .max_buf_size(65_536)
        .keep_alive(true)
        .serve_connection(TokioIo::new(stream), service);
    tokio::pin!(connection);
    let mut draining = *shutdown.borrow();
    let mut deadlines_open = true;
    if draining {
        connection.as_mut().graceful_shutdown();
    }

    loop {
        let deadline = *deadlines.borrow_and_update();
        tokio::select! {
            biased;
            changed = deadlines.changed(), if deadlines_open => {
                deadlines_open = changed.is_ok();
            }
            _ = shutdown.changed(), if !draining => {
                draining = true;
                connection.as_mut().graceful_shutdown();
            }
            result = &mut connection => {
                return result.map_err(|error| io::Error::other(error.to_string()));
            }
            _ = wait_for_deadline(deadline) => {
                // Polling Hyper can dispatch the next keepalive request and
                // publish a newer deadline in the same select iteration. Never
                // let an old, simultaneously-ready timer close that request.
                if deadlines.borrow().is_some_and(|latest| latest <= Instant::now()) {
                    // Returning drops Hyper, its socket, and the frontend body.
                    // Detached backend work retains its reservation while it
                    // drains, even when the transport was blocked in a write.
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "HTTP connection deadline exceeded",
                    ));
                }
            }
        }
    }
}

async fn wait_for_deadline(deadline: Option<Instant>) {
    match deadline {
        Some(deadline) => sleep_until(deadline).await,
        None => pending::<()>().await,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Config;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        time::{sleep, timeout},
    };

    fn gateway(request_timeout: Duration) -> Arc<Gateway> {
        Arc::new(Gateway::new(Config {
            endpoint: "127.0.0.1:0".into(),
            fpm_addr: "127.0.0.1:1".into(),
            script_filename: "/var/task/public/index.php".into(),
            document_root: "/var/task/public".into(),
            max_in_flight: 1,
            queue_capacity: 0,
            queue_timeout: Duration::from_secs(1),
            request_timeout,
            max_body_bytes: 1024,
            fpm_bin: "/unused/php-fpm".into(),
            fpm_config: "/unused/php-fpm.conf".into(),
            shutdown_grace: Duration::from_millis(500),
        }))
    }

    async fn assert_blocked_write_is_cut_off(start_shutdown: bool) {
        let (mut client, server) = tokio::io::duplex(1);
        let (shutdown, receiver) = watch::channel(false);
        let driver = tokio::spawn(serve(server, gateway(Duration::from_millis(20)), receiver));
        client
            .write_all(b"GET /__bref_web/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .await
            .unwrap();
        // Observing a response byte ensures the request was dispatched before
        // shutdown, rather than merely closing an idle transport.
        assert_eq!(client.read_u8().await.unwrap(), b'H');
        if start_shutdown {
            shutdown.send_replace(true);
        }
        // The response is larger than the one-byte transport capacity. The
        // client never reads, so Hyper must remain blocked on its socket write.
        let error = timeout(Duration::from_secs(1), driver)
            .await
            .expect("blocked transport was not closed")
            .unwrap()
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[tokio::test]
    async fn blocked_write_is_cut_off_without_polling_another_body_frame() {
        assert_blocked_write_is_cut_off(false).await;
    }

    #[tokio::test]
    async fn blocked_write_deadline_remains_active_during_graceful_shutdown() {
        assert_blocked_write_is_cut_off(true).await;
    }

    #[tokio::test]
    async fn keepalive_requests_refresh_the_connection_deadline() {
        let (mut client, server) = tokio::io::duplex(4096);
        let (_shutdown, receiver) = watch::channel(false);
        let driver = tokio::spawn(serve(server, gateway(Duration::from_millis(50)), receiver));
        let started = Instant::now();
        for _ in 0..6 {
            client
                .write_all(b"GET /__bref_web/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                .await
                .unwrap();
            let response = timeout(Duration::from_secs(1), async {
                let mut response = Vec::new();
                while !response.ends_with(b"0\r\n\r\n") {
                    response.push(client.read_u8().await.unwrap());
                }
                response
            })
            .await
            .unwrap();
            assert!(response.starts_with(b"HTTP/1.1 200"));
            sleep(Duration::from_millis(45)).await;
        }
        assert!(started.elapsed() > Duration::from_millis(150));
        drop(client);
        timeout(Duration::from_secs(1), driver)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }
}
