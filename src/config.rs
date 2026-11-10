use anyhow::{bail, Context, Result};
use std::{env, time::Duration};

#[derive(Debug, Clone)]
pub struct Config {
    pub endpoint: String,
    pub fpm_addr: String,
    pub script_filename: String,
    pub document_root: String,
    pub max_in_flight: usize,
    pub queue_capacity: usize,
    pub queue_timeout: Duration,
    pub request_timeout: Duration,
    /// FPM's `request_terminate_timeout` (whole seconds), as configured by the entrypoint.
    pub fpm_timeout: Duration,
    pub max_body_bytes: usize,
    pub fpm_bin: String,
    pub fpm_config: String,
    pub shutdown_grace: Duration,
    pub forwarded_headers: ForwardedHeaders,
}

/// How the gateway passes proxy headers to PHP (`BREF_WEB_FORWARDED_HEADERS`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ForwardedHeaders {
    /// As received, like Bref (default). Right behind another trusted proxy,
    /// such as CloudFront setting `X-Forwarded-Host` for a custom domain.
    Passthrough,
    /// Lambda's ingress is the only proxy: keep what it controls (the right-most
    /// `X-Forwarded-For` entry, `X-Forwarded-Proto`/`-Port`) and drop the
    /// forwarding and trace headers a client could otherwise set.
    Ingress,
}

impl ForwardedHeaders {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "passthrough" => Ok(Self::Passthrough),
            "ingress" => Ok(Self::Ingress),
            other => {
                bail!("BREF_WEB_FORWARDED_HEADERS must be passthrough or ingress, not {other:?}")
            }
        }
    }
}

impl Config {
    /// The defaults `from_env` uses, for tests to override field by field.
    #[cfg(test)]
    pub(crate) fn for_tests() -> Self {
        Self {
            endpoint: "127.0.0.1:8080".into(),
            fpm_addr: "127.0.0.1:9000".into(),
            script_filename: "/var/task/public/index.php".into(),
            document_root: "/var/task/public".into(),
            max_in_flight: 12,
            queue_capacity: 4,
            queue_timeout: Duration::from_secs(1),
            request_timeout: Duration::from_secs(30),
            fpm_timeout: Duration::from_secs(28),
            max_body_bytes: 6_291_456,
            fpm_bin: "/opt/bin/php-fpm".into(),
            fpm_config: "/tmp/bref-web/php-fpm.conf".into(),
            shutdown_grace: Duration::from_millis(500),
            forwarded_headers: ForwardedHeaders::Passthrough,
        }
    }

    pub fn from_env() -> Result<Self> {
        let max_in_flight = number("BREF_WEB_MAX_IN_FLIGHT", 12)?;
        if !(1..=128).contains(&max_in_flight) {
            bail!("BREF_WEB_MAX_IN_FLIGHT must be between 1 and 128");
        }
        let queue_capacity = number("BREF_WEB_QUEUE_CAPACITY", 4)?;
        if queue_capacity > 128 {
            bail!("BREF_WEB_QUEUE_CAPACITY must be at most 128");
        }
        let request_timeout_ms = number("BREF_WEB_REQUEST_TIMEOUT_MS", 30_000)?;
        if request_timeout_ms == 0 {
            bail!("BREF_WEB_REQUEST_TIMEOUT_MS must be positive");
        }
        let fpm_timeout_seconds = number("BREF_WEB_FPM_TIMEOUT_SECONDS", 28)?;
        if fpm_timeout_seconds == 0 {
            bail!("BREF_WEB_FPM_TIMEOUT_SECONDS must be positive");
        }
        Ok(Self {
            endpoint: value("AWS_LAMBDA_HTTP_ENDPOINT", "0.0.0.0:8080"),
            fpm_addr: value("BREF_WEB_FPM_ADDR", "127.0.0.1:9000"),
            script_filename: value("BREF_WEB_SCRIPT_FILENAME", "/var/task/public/index.php"),
            document_root: value("BREF_WEB_DOCUMENT_ROOT", "/var/task/public"),
            max_in_flight,
            queue_capacity,
            queue_timeout: Duration::from_millis(number("BREF_WEB_QUEUE_TIMEOUT_MS", 1000)? as u64),
            request_timeout: Duration::from_millis(request_timeout_ms as u64),
            fpm_timeout: Duration::from_secs(fpm_timeout_seconds as u64),
            max_body_bytes: number("BREF_WEB_MAX_BODY_BYTES", 6_291_456)?,
            fpm_bin: value("BREF_WEB_FPM_BIN", "/opt/bin/php-fpm"),
            fpm_config: value("BREF_WEB_FPM_CONFIG", "/tmp/bref-web/php-fpm.conf"),
            shutdown_grace: Duration::from_millis(number("BREF_WEB_SHUTDOWN_GRACE_MS", 500)? as u64),
            forwarded_headers: ForwardedHeaders::parse(&value(
                "BREF_WEB_FORWARDED_HEADERS",
                "passthrough",
            ))?,
        })
    }
}

fn value(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.into())
}
fn number(name: &str, default: usize) -> Result<usize> {
    match env::var(name) {
        Ok(v) => v.parse().with_context(|| format!("invalid {name}")),
        Err(_) => Ok(default),
    }
}

#[cfg(test)]
mod tests {
    use super::ForwardedHeaders;

    #[test]
    fn forwarded_headers_accepts_only_known_modes() {
        assert_eq!(
            ForwardedHeaders::parse("passthrough").unwrap(),
            ForwardedHeaders::Passthrough
        );
        assert_eq!(
            ForwardedHeaders::parse("ingress").unwrap(),
            ForwardedHeaders::Ingress
        );
        assert!(ForwardedHeaders::parse("Ingress").is_err());
        assert!(ForwardedHeaders::parse("").is_err());
    }
}
