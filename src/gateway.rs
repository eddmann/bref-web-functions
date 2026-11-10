use crate::{cgi, config::Config, fcgi};
use bytes::{Bytes, BytesMut};
use http::{header, HeaderValue, Method, Response, StatusCode};
use http_body::{Body, Frame};
use http_body_util::BodyExt;
use hyper::{body::Incoming, Request};
use std::{
    convert::Infallible,
    future::{pending, Future},
    io,
    pin::Pin,
    sync::{Arc, Mutex},
    task::{Context, Poll},
    time::Duration,
};
use tokio::{
    io::{AsyncRead, AsyncWriteExt},
    net::TcpStream,
    sync::{mpsc, oneshot, watch, OwnedSemaphorePermit, Semaphore},
    time::{timeout, timeout_at, Instant},
};

type BodyItem = Result<Frame<Bytes>, io::Error>;
pub struct GatewayBody {
    receiver: mpsc::Receiver<BodyItem>,
    terminal: Arc<Mutex<Option<String>>>,
    finished: bool,
    deadline: Option<Pin<Box<tokio::time::Sleep>>>,
}

impl Body for GatewayBody {
    type Data = Bytes;
    type Error = io::Error;
    fn poll_frame(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<BodyItem>> {
        let body = self.get_mut();
        if body.finished {
            return Poll::Ready(None);
        }
        if body
            .deadline
            .as_mut()
            .is_some_and(|timer| timer.as_mut().poll(cx).is_ready())
        {
            body.finished = true;
            body.receiver.close();
            return Poll::Ready(Some(Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "HTTP response deadline exceeded",
            ))));
        }
        let failure = body
            .terminal
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .take();
        if let Some(message) = failure {
            body.finished = true;
            body.receiver.close();
            return Poll::Ready(Some(Err(io::Error::other(message))));
        }
        match body.receiver.poll_recv(cx) {
            Poll::Ready(None) => {
                body.finished = true;
                let error = body
                    .terminal
                    .lock()
                    .unwrap_or_else(|p| p.into_inner())
                    .take();
                Poll::Ready(error.map(|message| Err(io::Error::other(message))))
            }
            other => other,
        }
    }
}

pub struct Gateway {
    pub config: Arc<Config>,
    pub workers: Arc<Semaphore>,
    slots: Arc<Semaphore>,
    admission: Arc<Admission>,
}

struct Admission {
    closed: watch::Sender<bool>,
    retirement: watch::Sender<Option<String>>,
}

impl Admission {
    fn is_open(&self) -> bool {
        !*self.closed.borrow()
    }

    fn close(&self) {
        self.closed.send_replace(true);
    }

    fn retire(&self, reason: String) {
        self.close();
        // The first unresolved backend determines retirement. Later failures
        // must not replace the reason observed by the process supervisor.
        self.retirement.send_if_modified(|current| {
            if current.is_some() {
                false
            } else {
                *current = Some(reason);
                true
            }
        });
    }
}

enum AdmissionError {
    Closed,
    Expired,
}

impl Gateway {
    pub fn new(config: Config) -> Self {
        let total = config.max_in_flight.saturating_add(config.queue_capacity);
        let (closed, _) = watch::channel(false);
        let (retirement, _) = watch::channel(None);
        Self {
            workers: Arc::new(Semaphore::new(config.max_in_flight)),
            slots: Arc::new(Semaphore::new(total)),
            config: Arc::new(config),
            admission: Arc::new(Admission { closed, retirement }),
        }
    }

    pub fn retirement(&self) -> watch::Receiver<Option<String>> {
        self.admission.retirement.subscribe()
    }

    pub fn close_admission(&self) {
        self.admission.close();
        self.slots.close();
    }

    async fn acquire_worker(
        &self,
        deadline: Instant,
    ) -> Result<OwnedSemaphorePermit, AdmissionError> {
        let mut closed = self.admission.closed.subscribe();
        tokio::select! {
            biased;
            _ = closed.wait_for(|closed| *closed) => Err(AdmissionError::Closed),
            result = timeout_at(deadline, self.workers.clone().acquire_owned()) => {
                match result {
                    Ok(Ok(permit)) => Ok(permit),
                    _ => Err(AdmissionError::Expired),
                }
            }
        }
    }

    /// `request_id` comes from [`request_id`], so connection-level log lines can
    /// name the same request.
    pub async fn handle(
        self: Arc<Self>,
        request: Request<Incoming>,
        ingress: Instant,
        request_id: String,
    ) -> Result<Response<GatewayBody>, Infallible> {
        let deadline = ingress + self.config.request_timeout;
        let health =
            request.method() == Method::GET && request.uri().path() == "/__bref_web/health";
        if health {
            if !self.admission.is_open() {
                return Ok(unavailable("runtime admission closed\n"));
            }
            return Ok(simple(StatusCode::OK, "ready\n"));
        }
        let reject = |response: Response<GatewayBody>, reason: &str| {
            // PHP-served requests appear in FPM's access log; these never reach PHP.
            crate::log_line!(
                "bref-web request_id={request_id} gateway_status={} reason={reason} elapsed_ms={}",
                response.status().as_u16(),
                ingress.elapsed().as_millis()
            );
            Ok(response)
        };
        if !self.admission.is_open() {
            return reject(
                unavailable("runtime admission closed\n"),
                "admission_closed",
            );
        }
        let Ok(slot) = self.slots.clone().try_acquire_owned() else {
            return reject(unavailable("runtime queue is full\n"), "queue_full");
        };
        let worker = match self
            .acquire_worker(deadline.min(ingress + self.config.queue_timeout))
            .await
        {
            Ok(permit) => permit,
            Err(AdmissionError::Closed) => {
                return reject(
                    unavailable("runtime admission closed\n"),
                    "admission_closed",
                )
            }
            Err(AdmissionError::Expired) => {
                return reject(
                    unavailable("runtime queue wait expired\n"),
                    "queue_wait_expired",
                )
            }
        };
        if !self.admission.is_open() {
            return reject(
                unavailable("runtime admission closed\n"),
                "admission_closed",
            );
        }
        // Admission precedes collection: at most max_in_flight full input bodies exist.
        let (parts, mut incoming) = request.into_parts();
        let mut collected = BytesMut::new();
        let collect = async {
            while let Some(frame) = incoming.frame().await {
                let frame = frame.map_err(|e| io::Error::other(e.to_string()))?;
                if let Ok(data) = frame.into_data() {
                    if data.len() > self.config.max_body_bytes.saturating_sub(collected.len()) {
                        return Err(io::Error::new(
                            io::ErrorKind::FileTooLarge,
                            "request body exceeds configured limit",
                        ));
                    }
                    collected.extend_from_slice(&data);
                }
            }
            Ok::<(), io::Error>(())
        };
        match timeout_at(deadline, collect).await {
            Err(_) => {
                return reject(
                    simple(StatusCode::REQUEST_TIMEOUT, "request body timed out\n"),
                    "body_timeout",
                )
            }
            Ok(Err(error)) if error.kind() == io::ErrorKind::FileTooLarge => {
                return reject(
                    simple(
                        StatusCode::PAYLOAD_TOO_LARGE,
                        "request body exceeds configured limit\n",
                    ),
                    "body_too_large",
                )
            }
            Ok(Err(_)) => {
                return reject(
                    simple(StatusCode::BAD_REQUEST, "invalid request body\n"),
                    "body_invalid",
                )
            }
            Ok(Ok(())) => {}
        }
        if !self.admission.is_open() {
            return reject(
                unavailable("runtime admission closed\n"),
                "admission_closed",
            );
        }
        let params = cgi::params(
            &self.config,
            &parts.method,
            &parts.uri,
            &parts.headers,
            collected.len(),
            &request_id,
        );
        let (head_sender, head_receiver) = oneshot::channel();
        let (body_sender, body_receiver) = mpsc::channel(4);
        let terminal = Arc::new(Mutex::new(None));
        let task_terminal = terminal.clone();
        let addr = self.config.fpm_addr.clone();
        let method = parts.method;
        let log_id = request_id.clone();
        let admission = self.admission.clone();
        // Separate from the frontend deadline: an HTTP timeout does not stop PHP.
        // Downstream backpressure may retain backend output until the HTTP
        // cutoff closes its receiver. Leave confirmation time after both that
        // deadline and FPM's own script cap, rather than retiring first merely
        // because a frontend is slow.
        let accounting_deadline =
            confirmation_deadline(deadline, Instant::now(), self.config.fpm_timeout);
        tokio::spawn(async move {
            // These permits belong to backend work, independently of HTTP body ownership.
            let _worker = worker;
            let _slot = slot;
            let mut head_sender = Some(head_sender);
            let result = timeout_at(accounting_deadline, async {
                let stream = TcpStream::connect(&addr).await?;
                let _ = stream.set_nodelay(true);
                let (mut read_half, mut write_half) = tokio::io::split(stream);
                let result = {
                    let write = fcgi::write_request(&mut write_half, &params, &collected);
                    let read = relay_response(
                    &mut read_half,
                    &mut head_sender,
                    &body_sender,
                    &method,
                    &log_id,
                );
                    tokio::pin!(write, read);
                // Reading must proceed while writing; an early backend response cancels remaining writes.
                    tokio::select! {
                        result = &mut read => result,
                        result = &mut write => { match result { Ok(()) => read.await, Err(error) => Err(error) } }
                    }
                };
                if let Err(error) = &result {
                    publish_failure(error, &mut head_sender, &body_sender, &task_terminal);
                    // At an early CGI/protocol error PHP can still be running. Half-close input,
                    // then drain raw output to actual socket EOF before releasing its reservation.
                    let _ = write_half.shutdown().await;
                    let _ = tokio::io::copy(&mut read_half, &mut tokio::io::sink()).await;
                }
                result
            })
            .await;
            let result = match result {
                Ok(result) => result,
                Err(_) => {
                    let error = io::Error::new(
                        io::ErrorKind::TimedOut,
                        "backend termination not confirmed within accounting budget",
                    );
                    publish_failure(&error, &mut head_sender, &body_sender, &task_terminal);
                    admission.retire(format!(
                        "backend completion unconfirmed request_id={log_id}"
                    ));
                    crate::log_line!("bref-web request_id={log_id} unresolved_backend=true admission_closed=true");
                    drop(body_sender);
                    drop(collected);
                    drop(params);
                    // Deliberately retain the permits until process shutdown. Do not oversubscribe
                    // a PHP worker whose termination could not be confirmed, or restart the pool.
                    pending::<()>().await;
                    return;
                }
            };
            if let Err(error) = result {
                crate::log_line!("bref-web request_id={log_id} backend_error={error}");
                publish_failure(&error, &mut head_sender, &body_sender, &task_terminal);
            }
            drop(body_sender);
        });
        let head = match timeout_at(deadline, head_receiver).await {
            Ok(Ok(Ok(head))) => head,
            Ok(Ok(Err(status))) => {
                return reject(
                    simple(status, "PHP backend request failed\n"),
                    "backend_failed",
                )
            }
            Ok(Err(_)) => {
                return reject(
                    simple(StatusCode::BAD_GATEWAY, "PHP backend stopped\n"),
                    "backend_stopped",
                )
            }
            Err(_) => {
                return reject(
                    simple(
                        StatusCode::GATEWAY_TIMEOUT,
                        "HTTP request deadline exceeded\n",
                    ),
                    "response_head_timeout",
                )
            }
        };
        let mut response = Response::new(GatewayBody {
            receiver: body_receiver,
            terminal,
            finished: false,
            deadline: Some(Box::pin(tokio::time::sleep_until(deadline))),
        });
        *response.status_mut() = head.status;
        *response.headers_mut() = head.headers;
        if let Ok(value) = HeaderValue::from_str(&request_id) {
            response
                .headers_mut()
                .insert("x-bref-web-request-id", value);
        }
        Ok(response)
    }

    pub async fn drain(&self, budget: Duration) {
        let _ = timeout(
            budget,
            self.workers
                .clone()
                .acquire_many_owned(self.config.max_in_flight as u32),
        )
        .await;
    }
}

/// The supplied Lambda request ID, or a local UUID when it is missing or could
/// break out of a log field. The ID reaches FPM's space-separated access log,
/// the PHP environment and a response header, so only token characters pass.
pub fn request_id(header: Option<&HeaderValue>) -> String {
    header
        .map(HeaderValue::as_bytes)
        .filter(|id| !id.is_empty() && id.len() <= 256)
        .filter(|id| {
            id.iter()
                .all(|&b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b':'))
        })
        .map(|id| String::from_utf8_lossy(id).into_owned())
        .unwrap_or_else(|| format!("local-{}", uuid::Uuid::new_v4()))
}

/// FPM checks `request_terminate_timeout` only on its heartbeat, every
/// timeout / 3, so it ends an overrunning worker up to a third of the timeout
/// late (28 s → 37.3 s). Wait for that before treating completion as unconfirmed.
fn confirmation_deadline(
    frontend_deadline: Instant,
    backend_start: Instant,
    fpm_timeout: Duration,
) -> Instant {
    frontend_deadline.max(backend_start + fpm_timeout + fpm_timeout / 3) + Duration::from_secs(2)
}

type HeadSender = oneshot::Sender<Result<cgi::Head, StatusCode>>;

fn publish_failure(
    error: &io::Error,
    head_sender: &mut Option<HeadSender>,
    body_sender: &mpsc::Sender<BodyItem>,
    terminal: &Arc<Mutex<Option<String>>>,
) {
    if let Some(sender) = head_sender.take() {
        let status = if error.kind() == io::ErrorKind::TimedOut {
            StatusCode::GATEWAY_TIMEOUT
        } else {
            StatusCode::BAD_GATEWAY
        };
        let _ = sender.send(Err(status));
    } else {
        *terminal.lock().unwrap_or_else(|p| p.into_inner()) = Some(error.to_string());
        let _ = body_sender.try_send(Err(io::Error::other(error.to_string())));
    }
}

async fn relay_response<R: AsyncRead + Unpin>(
    reader: &mut R,
    head_sender: &mut Option<HeadSender>,
    output: &mpsc::Sender<BodyItem>,
    method: &Method,
    request_id: &str,
) -> io::Result<()> {
    let mut header_buffer = Vec::new();
    let mut parsed = false;
    let mut body_allowed = true;
    let mut expected = None;
    let mut observed = 0u64;
    loop {
        let record = fcgi::read_record(reader).await?;
        match record.kind {
            fcgi::STDERR => {
                // Record payloads are bounded by the protocol; never accumulate stderr.
                if !record.content.is_empty() {
                    crate::log_line!(
                        "bref-web request_id={request_id} php_stderr={}",
                        String::from_utf8_lossy(&record.content)
                    );
                }
            }
            fcgi::STDOUT => {
                let data = if !parsed {
                    header_buffer.extend_from_slice(&record.content);
                    let Some(mut head) = cgi::parse_head(&header_buffer)? else {
                        continue;
                    };
                    body_allowed = *method != Method::HEAD
                        && head.status != StatusCode::NO_CONTENT
                        && head.status != StatusCode::NOT_MODIFIED;
                    if head.status == StatusCode::NO_CONTENT {
                        head.headers.remove(header::CONTENT_LENGTH);
                    }
                    expected = if body_allowed {
                        head.content_length
                    } else {
                        None
                    };
                    let remainder = head.remainder.clone();
                    if let Some(sender) = head_sender.take() {
                        let _ = sender.send(Ok(head));
                    }
                    parsed = true;
                    header_buffer.clear();
                    remainder
                } else {
                    Bytes::from(record.content)
                };
                if !body_allowed {
                    continue;
                }
                observed = observed
                    .checked_add(data.len() as u64)
                    .ok_or_else(|| io::Error::other("response length overflow"))?;
                if expected.is_some_and(|length| observed > length) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "response exceeded Content-Length",
                    ));
                }
                if !data.is_empty() && !output.is_closed() {
                    // Bounded channel blocks socket reads under downstream backpressure.
                    // If the client leaves, discard output while keeping the worker accounted for until END/error.
                    let _ = output.send(Ok(Frame::data(data))).await;
                }
            }
            fcgi::END_REQUEST => {
                let app_status = fcgi::end_status(&record)?;
                if !parsed {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "PHP ended before producing CGI headers",
                    ));
                }
                if expected.is_some_and(|length| observed != length) {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "response shorter than Content-Length",
                    ));
                }
                if app_status != 0 {
                    crate::log_line!(
                        "bref-web request_id={request_id} php_app_status={app_status}"
                    );
                }
                return Ok(());
            }
            _ => unreachable!(),
        }
    }
}

fn simple(status: StatusCode, message: &'static str) -> Response<GatewayBody> {
    let (sender, receiver) = mpsc::channel(1);
    let _ = sender.try_send(Ok(Frame::data(Bytes::from_static(message.as_bytes()))));
    drop(sender);
    let mut response = Response::new(GatewayBody {
        receiver,
        terminal: Arc::new(Mutex::new(None)),
        finished: false,
        deadline: None,
    });
    *response.status_mut() = status;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response
}
fn unavailable(message: &'static str) -> Response<GatewayBody> {
    let mut response = simple(StatusCode::SERVICE_UNAVAILABLE, message);
    response
        .headers_mut()
        .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncWriteExt;

    fn record(kind: u8, bytes: &[u8]) -> Vec<u8> {
        let length = u16::try_from(bytes.len()).unwrap().to_be_bytes();
        let mut out = vec![1, kind, 0, 1, length[0], length[1], 0, 0];
        out.extend_from_slice(bytes);
        out
    }

    fn gateway() -> Arc<Gateway> {
        Arc::new(Gateway::new(Config {
            fpm_addr: "127.0.0.1:0".into(),
            max_in_flight: 1,
            queue_capacity: 1,
            queue_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(5),
            max_body_bytes: 1024,
            ..Config::for_tests()
        }))
    }

    #[tokio::test]
    async fn closing_admission_wakes_queued_work_without_releasing_backend_reservation() {
        let gateway = gateway();
        let active = gateway.workers.clone().acquire_owned().await.unwrap();
        let queued_gateway = gateway.clone();
        let queued = tokio::spawn(async move {
            queued_gateway
                .acquire_worker(Instant::now() + Duration::from_secs(5))
                .await
        });
        tokio::task::yield_now().await;
        assert!(!queued.is_finished());
        gateway.close_admission();
        let result = timeout(Duration::from_millis(100), queued)
            .await
            .unwrap()
            .unwrap();
        assert!(matches!(result, Err(AdmissionError::Closed)));
        assert_eq!(gateway.workers.available_permits(), 0);
        assert_eq!(*gateway.retirement().borrow(), None);
        assert!(gateway.slots.is_closed());
        drop(active);
        assert_eq!(gateway.workers.available_permits(), 1);
        assert!(matches!(
            gateway
                .acquire_worker(Instant::now() + Duration::from_secs(1))
                .await,
            Err(AdmissionError::Closed)
        ));
        assert_eq!(gateway.workers.available_permits(), 1);
    }

    #[tokio::test]
    async fn retirement_notifies_supervisor_and_preserves_the_first_reason() {
        let gateway = gateway();
        let active = gateway.workers.clone().acquire_owned().await.unwrap();
        let mut retirement = gateway.retirement();
        let queued_gateway = gateway.clone();
        let queued = tokio::spawn(async move {
            queued_gateway
                .acquire_worker(Instant::now() + Duration::from_secs(5))
                .await
        });
        tokio::task::yield_now().await;
        gateway.admission.retire("first unresolved backend".into());
        gateway.admission.retire("later failure".into());
        timeout(Duration::from_millis(100), retirement.changed())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            retirement.borrow().as_deref(),
            Some("first unresolved backend")
        );
        assert_eq!(gateway.workers.available_permits(), 0);
        assert!(matches!(
            timeout(Duration::from_millis(100), queued)
                .await
                .unwrap()
                .unwrap(),
            Err(AdmissionError::Closed)
        ));
        assert!(!gateway.admission.is_open());
        drop(active);
    }

    #[test]
    fn confirmation_budget_follows_the_later_frontend_or_fpm_deadline() {
        let start = Instant::now();
        // FPM's heartbeat-late termination (3 s + 1 s) is later than the frontend.
        assert_eq!(
            confirmation_deadline(
                start + Duration::from_millis(150),
                start,
                Duration::from_secs(3),
            ),
            start + Duration::from_secs(6)
        );
        assert_eq!(
            confirmation_deadline(
                start + Duration::from_secs(10),
                start,
                Duration::from_secs(2),
            ),
            start + Duration::from_secs(12)
        );
        // The deployed defaults: FPM may end a 28 s overrun at 37.33 s, after
        // the 30 s HTTP deadline plus grace; retiring at 32 s would be early.
        assert_eq!(
            confirmation_deadline(
                start + Duration::from_secs(30),
                start,
                Duration::from_secs(28),
            ),
            start + Duration::from_nanos(39_333_333_333)
        );
    }

    #[tokio::test]
    async fn streams_head_before_end_and_keeps_backend_permit_after_client_drop() {
        let workers = Arc::new(Semaphore::new(1));
        let permit = workers.clone().acquire_owned().await.unwrap();
        let (mut producer, mut reader) = tokio::io::duplex(128);
        let (head_tx, head_rx) = oneshot::channel();
        let (body_tx, body_rx) = mpsc::channel(1);
        let (allow_end, wait_end) = oneshot::channel();
        let backend = tokio::spawn(async move {
            producer
                .write_all(&record(6, b"Content-Type: text/plain\r\n\r\nabc"))
                .await
                .unwrap();
            producer.write_all(&record(6, b"def")).await.unwrap();
            wait_end.await.unwrap();
            producer.write_all(&record(3, &[0; 8])).await.unwrap();
        });
        let relay = tokio::spawn(async move {
            let _permit = permit;
            relay_response(
                &mut reader,
                &mut Some(head_tx),
                &body_tx,
                &Method::GET,
                "test",
            )
            .await
        });
        let head = timeout(Duration::from_secs(1), head_rx)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(head.status, 200);
        assert_eq!(workers.available_permits(), 0);
        drop(body_rx);
        sleep_short().await;
        assert!(
            !relay.is_finished(),
            "HTTP cancellation must not make backend work disappear"
        );
        assert_eq!(workers.available_permits(), 0);
        allow_end.send(()).unwrap();
        timeout(Duration::from_secs(1), relay)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        backend.await.unwrap();
        assert_eq!(workers.available_permits(), 1);
    }

    #[test]
    fn request_id_keeps_lambda_ids_and_replaces_unsafe_values() {
        let id = "8f5c2f8a-1c2b-4d3e-9f00-0123456789ab";
        assert_eq!(request_id(Some(&HeaderValue::from_static(id))), id);
        for unsafe_id in ["abc status=200", "a\"b", "a\tb", ""] {
            let replaced = request_id(Some(&HeaderValue::from_str(unsafe_id).unwrap()));
            assert!(
                replaced.starts_with("local-"),
                "{unsafe_id:?} -> {replaced}"
            );
        }
        let long = HeaderValue::from_str(&"a".repeat(257)).unwrap();
        assert!(request_id(Some(&long)).starts_with("local-"));
        assert!(request_id(None).starts_with("local-"));
    }

    #[tokio::test]
    async fn missing_end_and_wrong_content_length_fail_stream() {
        let mut bytes = record(6, b"Content-Type: text/plain\r\n\r\nabc");
        let (head_tx, _) = oneshot::channel();
        let (body_tx, _) = mpsc::channel(1);
        assert!(relay_response(
            &mut bytes.as_slice(),
            &mut Some(head_tx),
            &body_tx,
            &Method::GET,
            "test"
        )
        .await
        .is_err());
        bytes = record(6, b"Content-Length: 4\r\n\r\nabc");
        bytes.extend(record(3, &[0; 8]));
        let (head_tx, _) = oneshot::channel();
        let error = relay_response(
            &mut bytes.as_slice(),
            &mut Some(head_tx),
            &body_tx,
            &Method::GET,
            "test",
        )
        .await
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::UnexpectedEof);
    }

    #[tokio::test]
    async fn http_body_deadline_ends_the_response_stream() {
        let (_sender, receiver) = mpsc::channel(1);
        let mut body = GatewayBody {
            receiver,
            terminal: Arc::new(Mutex::new(None)),
            finished: false,
            deadline: Some(Box::pin(tokio::time::sleep(Duration::from_millis(5)))),
        };
        let error = timeout(Duration::from_secs(1), body.frame())
            .await
            .unwrap()
            .unwrap()
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    async fn sleep_short() {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}
