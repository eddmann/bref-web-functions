pub mod cgi;
pub mod config;
pub mod connection;
pub mod fcgi;
pub mod gateway;

/// Write one log line to stderr with a single `write`. `eprintln!` issues a
/// write per formatted piece, so PHP-FPM's master, which shares stderr, could
/// land its own line inside ours. Lines below the pipe
/// buffer size (4 KiB) are then never split.
#[macro_export]
macro_rules! log_line {
    ($($arg:tt)*) => {{
        use std::io::Write as _;
        let mut line = format!($($arg)*);
        line.push('\n');
        let _ = std::io::stderr().lock().write_all(line.as_bytes());
    }};
}
