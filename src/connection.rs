//! Drive HTTP/1 connections with a deadline independent of response-body polling.
use crate::gateway::{request_id, Gateway};
use hyper::{server::conn::http1, service::service_fn};
use hyper_util::rt::{TokioIo, TokioTimer};
use std::{fmt, future::pending, io, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    sync::watch,
    time::{sleep_until, Instant},
};

// Let a handler flush its terminal 408/504 response at the HTTP deadline. A
// blocked transport still has a finite cutoff, including after the last body
// frame was polled. This grace does not extend the PHP request deadline.
pub const RESPONSE_FLUSH_GRACE: Duration = Duration::from_millis(100);

/// A connection that ended with an error, naming the last request it carried.
#[derive(Debug)]
pub struct ConnectionError {
    pub request_id: Option<String>,
    pub error: io::Error,
}

impl fmt::Display for ConnectionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let request_id = self.request_id.as_deref().unwrap_or("-");
        write!(
            f,
            "request_id={request_id} http_connection_error={}",
            self.error
        )
    }
}

pub async fn serve<S>(
    stream: S,
    gateway: Arc<Gateway>,
    mut shutdown: watch::Receiver<bool>,
) -> Result<(), ConnectionError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (deadline_sender, mut deadlines) = watch::channel::<Option<Instant>>(None);
    let (request_sender, last_request) = watch::channel::<Option<String>>(None);
    let header_timeout = gateway.config.request_timeout + RESPONSE_FLUSH_GRACE;
    let service = service_fn(move |request: hyper::Request<_>| {
        let ingress = Instant::now();
        deadline_sender.send_replace(Some(ingress + header_timeout));
        let id = request_id(request.headers().get("lambda-web-request-id"));
        request_sender.send_replace(Some(id.clone()));
        gateway.clone().handle(request, ingress, id)
    });
    let failed = |error: io::Error| ConnectionError {
        request_id: last_request.borrow().clone(),
        error,
    };
    // The request watchdog below starts only once Hyper dispatches a request.
    // Hyper's header timer covers the time before that: a new connection that
    // sends nothing, incomplete headers, and the wait for the next keepalive
    // request. It needs a timer to take effect.
    let connection = http1::Builder::new()
        .max_buf_size(65_536)
        .timer(TokioTimer::new())
        .header_read_timeout(header_timeout)
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
                return result.map_err(|error| failed(io::Error::other(error.to_string())));
            }
            _ = wait_for_deadline(deadline) => {
                // Polling Hyper can dispatch the next keepalive request and
                // publish a newer deadline in the same select iteration. Never
                // let an old, simultaneously-ready timer close that request.
                if deadlines.borrow().is_some_and(|latest| latest <= Instant::now()) {
                    // Returning drops Hyper, its socket, and the frontend body.
                    // Detached backend work retains its reservation while it
                    // drains, even when the transport was blocked in a write.
                    return Err(failed(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "HTTP connection deadline exceeded",
                    )));
                }
            }
        }
    }
}

/// What the accept loop does after `accept()` fails.
#[derive(Debug, PartialEq, Eq)]
pub enum AcceptFailure {
    /// One pending connection failed; the listener is fine.
    Retry,
    /// Out of descriptors, buffers or memory. Existing connections free them as
    /// they finish, so pause instead of retiring a healthy runtime.
    /// Pause for [`ACCEPT_PAUSE`].
    Pause,
    /// The listener itself is unusable; stop so the platform can replace it.
    Fatal,
}

pub const ACCEPT_PAUSE: Duration = Duration::from_millis(100);

pub fn accept_failure(error: &io::Error) -> AcceptFailure {
    use nix::errno::Errno;
    if matches!(
        error.kind(),
        io::ErrorKind::ConnectionAborted
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::ConnectionRefused
            | io::ErrorKind::Interrupted
    ) {
        return AcceptFailure::Retry;
    }
    let exhausted = [Errno::EMFILE, Errno::ENFILE, Errno::ENOBUFS, Errno::ENOMEM];
    match error.raw_os_error() {
        Some(code) if exhausted.iter().any(|errno| *errno as i32 == code) => AcceptFailure::Pause,
        _ => AcceptFailure::Fatal,
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
            fpm_addr: "127.0.0.1:1".into(),
            max_in_flight: 1,
            queue_capacity: 0,
            request_timeout,
            max_body_bytes: 1024,
            ..Config::for_tests()
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
        assert_eq!(error.error.kind(), io::ErrorKind::TimedOut);
        assert!(error.request_id.is_some_and(|id| id.starts_with("local-")));
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

    async fn assert_closed_before_dispatch(sent: &[u8]) {
        let (mut client, server) = tokio::io::duplex(4096);
        let (_shutdown, receiver) = watch::channel(false);
        let started = Instant::now();
        let driver = tokio::spawn(serve(server, gateway(Duration::from_millis(50)), receiver));
        client.write_all(sent).await.unwrap();
        timeout(Duration::from_secs(1), driver)
            .await
            .expect("connection without a complete request head was not closed")
            .unwrap()
            .unwrap_err();
        assert!(started.elapsed() >= Duration::from_millis(150));
        let mut rest = Vec::new();
        client.read_to_end(&mut rest).await.unwrap();
        assert!(
            !rest.starts_with(b"HTTP/1.1 200"),
            "no request was dispatched"
        );
    }

    #[test]
    fn accept_failures_pause_on_exhaustion_and_stop_only_when_unexpected() {
        use nix::errno::Errno;
        let os = |errno: Errno| io::Error::from_raw_os_error(errno as i32);
        for errno in [Errno::EMFILE, Errno::ENFILE, Errno::ENOBUFS, Errno::ENOMEM] {
            assert_eq!(accept_failure(&os(errno)), AcceptFailure::Pause, "{errno}");
        }
        for errno in [Errno::ECONNABORTED, Errno::ECONNRESET, Errno::EINTR] {
            assert_eq!(accept_failure(&os(errno)), AcceptFailure::Retry, "{errno}");
        }
        for errno in [Errno::EBADF, Errno::EINVAL, Errno::ENOTSOCK] {
            assert_eq!(accept_failure(&os(errno)), AcceptFailure::Fatal, "{errno}");
        }
    }

    #[tokio::test]
    async fn connection_that_sends_nothing_is_closed() {
        assert_closed_before_dispatch(b"").await;
    }

    #[tokio::test]
    async fn incomplete_request_head_is_closed() {
        assert_closed_before_dispatch(b"GET /__bref_web/health HTTP/1.1\r\nHost: loc").await;
    }
}
