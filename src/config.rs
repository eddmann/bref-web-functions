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
    pub max_body_bytes: usize,
    pub fpm_bin: String,
    pub fpm_config: String,
    pub shutdown_grace: Duration,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        let max_in_flight = number("BREF_WEB_MAX_IN_FLIGHT", 4)?;
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
        Ok(Self {
            endpoint: value("AWS_LAMBDA_HTTP_ENDPOINT", "0.0.0.0:8080"),
            fpm_addr: value("BREF_WEB_FPM_ADDR", "127.0.0.1:9000"),
            script_filename: value("BREF_WEB_SCRIPT_FILENAME", "/var/task/public/index.php"),
            document_root: value("BREF_WEB_DOCUMENT_ROOT", "/var/task/public"),
            max_in_flight,
            queue_capacity,
            queue_timeout: Duration::from_millis(number("BREF_WEB_QUEUE_TIMEOUT_MS", 1000)? as u64),
            request_timeout: Duration::from_millis(request_timeout_ms as u64),
            max_body_bytes: number("BREF_WEB_MAX_BODY_BYTES", 6_291_456)?,
            fpm_bin: value("BREF_WEB_FPM_BIN", "/opt/bin/php-fpm"),
            fpm_config: value("BREF_WEB_FPM_CONFIG", "/tmp/bref-web/php-fpm.conf"),
            shutdown_grace: Duration::from_millis(number("BREF_WEB_SHUTDOWN_GRACE_MS", 500)? as u64),
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
