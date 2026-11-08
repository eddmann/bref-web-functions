use crate::config::Config;
use bytes::Bytes;
use http::{header, HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri};
use std::{
    io,
    path::{Component, Path},
};

pub const MAX_HEADER_BYTES: usize = 65_536;
const MAX_HEADERS: usize = 128;

pub struct Head {
    pub status: StatusCode,
    pub headers: HeaderMap,
    pub remainder: Bytes,
    pub content_length: Option<u64>,
}

pub fn parse_head(buffer: &[u8]) -> io::Result<Option<Head>> {
    let terminator = buffer
        .windows(4)
        .position(|b| b == b"\r\n\r\n")
        .map(|i| (i, 4))
        .or_else(|| buffer.windows(2).position(|b| b == b"\n\n").map(|i| (i, 2)));
    let Some((offset, width)) = terminator else {
        if buffer.len() > MAX_HEADER_BYTES {
            return Err(invalid("CGI response headers too large"));
        }
        return Ok(None);
    };
    if offset + width > MAX_HEADER_BYTES {
        return Err(invalid("CGI response headers too large"));
    }
    let mut slots = [httparse::EMPTY_HEADER; MAX_HEADERS];
    let parsed = httparse::parse_headers(&buffer[..offset + width], &mut slots)
        .map_err(|_| invalid("malformed CGI response headers"))?;
    let httparse::Status::Complete((_, fields)) = parsed else {
        return Err(invalid("incomplete CGI response headers"));
    };
    let mut status = None;
    let mut headers = HeaderMap::new();
    for field in fields {
        if field.name.eq_ignore_ascii_case("status") {
            if status.is_some() {
                return Err(invalid("duplicate CGI status"));
            }
            let value =
                std::str::from_utf8(field.value).map_err(|_| invalid("invalid CGI status"))?;
            let code = value
                .split_ascii_whitespace()
                .next()
                .ok_or_else(|| invalid("missing CGI status"))?;
            status = Some(
                code.parse::<StatusCode>()
                    .map_err(|_| invalid("invalid CGI status"))?,
            );
        } else {
            headers.append(
                HeaderName::from_bytes(field.name.as_bytes())
                    .map_err(|_| invalid("invalid response header name"))?,
                HeaderValue::from_bytes(field.value)
                    .map_err(|_| invalid("invalid response header value"))?,
            );
        }
    }
    let content_length = response_length(&headers)?;
    if let Some(length) = content_length {
        headers.insert(
            header::CONTENT_LENGTH,
            HeaderValue::from_str(&length.to_string())
                .map_err(|_| invalid("invalid response length"))?,
        );
    }
    let status = status.unwrap_or_else(|| {
        if headers.contains_key(header::LOCATION) {
            StatusCode::FOUND
        } else {
            StatusCode::OK
        }
    });
    if status.is_informational() {
        return Err(invalid(
            "informational CGI responses are outside this prototype's contract",
        ));
    }
    strip_hop_by_hop(&mut headers);
    headers.remove("x-accel-buffering");
    Ok(Some(Head {
        status,
        headers,
        remainder: Bytes::copy_from_slice(&buffer[offset + width..]),
        content_length,
    }))
}

fn response_length(headers: &HeaderMap) -> io::Result<Option<u64>> {
    let mut length = None;
    for value in headers.get_all(header::CONTENT_LENGTH) {
        let current = value
            .to_str()
            .ok()
            .and_then(|s| s.parse::<u64>().ok())
            .ok_or_else(|| invalid("invalid response Content-Length"))?;
        if length.is_some_and(|previous| previous != current) {
            return Err(invalid("conflicting response Content-Length"));
        }
        length = Some(current);
    }
    Ok(length)
}

pub fn strip_hop_by_hop(headers: &mut HeaderMap) {
    let nominated: Vec<HeaderName> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(','))
        .filter_map(|v| HeaderName::from_bytes(v.trim().as_bytes()).ok())
        .collect();
    for name in nominated {
        headers.remove(name);
    }
    for name in [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ] {
        headers.remove(name);
    }
}

pub fn params(
    config: &Config,
    method: &Method,
    uri: &Uri,
    headers: &HeaderMap,
    length: usize,
    request_id: &str,
) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut result = Vec::new();
    let mut add =
        |name: &str, value: &[u8]| result.push((name.as_bytes().to_vec(), value.to_vec()));
    add("GATEWAY_INTERFACE", b"CGI/1.1");
    add("SERVER_SOFTWARE", b"bref-web-rust-prototype");
    add("SERVER_PROTOCOL", b"HTTP/1.1");
    add("REQUEST_METHOD", method.as_str().as_bytes());
    add(
        "REQUEST_URI",
        uri.path_and_query().map_or("/", |v| v.as_str()).as_bytes(),
    );
    add("QUERY_STRING", uri.query().unwrap_or("").as_bytes());
    add("DOCUMENT_ROOT", config.document_root.as_bytes());
    add("SCRIPT_FILENAME", config.script_filename.as_bytes());
    let script_name = script_name(&config.document_root, &config.script_filename);
    add("SCRIPT_NAME", script_name.as_bytes());
    add("DOCUMENT_URI", script_name.as_bytes());
    add("PATH_INFO", &decode_path(uri.path().as_bytes()));
    add("CONTENT_LENGTH", length.to_string().as_bytes());
    add("HTTPS", b"on");
    add("REQUEST_SCHEME", b"https");
    add("SERVER_PORT", b"443");
    add("REMOTE_ADDR", b"127.0.0.1");
    add("LAMBDA_REQUEST_ID", request_id.as_bytes());
    if let Some(host) = headers.get(header::HOST) {
        add("SERVER_NAME", host.as_bytes());
        add("HTTP_HOST", host.as_bytes());
    }
    if let Some(content_type) = headers.get(header::CONTENT_TYPE) {
        add("CONTENT_TYPE", content_type.as_bytes());
    }
    let mut clean = headers.clone();
    strip_hop_by_hop(&mut clean);
    for name in clean.keys() {
        if matches!(
            name.as_str(),
            "host" | "content-type" | "content-length" | "proxy"
        ) {
            continue;
        }
        let key = format!(
            "HTTP_{}",
            name.as_str().replace('-', "_").to_ascii_uppercase()
        );
        let separator = if name == header::COOKIE {
            b"; ".as_slice()
        } else {
            b", ".as_slice()
        };
        let mut combined = Vec::new();
        for value in clean.get_all(name) {
            if !combined.is_empty() {
                combined.extend_from_slice(separator);
            }
            combined.extend_from_slice(value.as_bytes());
        }
        add(&key, &combined);
    }
    result
}

/// Map the selected front controller to its public path without touching disk.
/// A controller outside the document root uses its basename at the URL root;
/// its filesystem location must never become part of the generated URL.
fn script_name(document_root: &str, script_filename: &str) -> String {
    let script = Path::new(script_filename);
    let relative = script
        .strip_prefix(Path::new(document_root))
        .ok()
        .filter(|path| !path.as_os_str().is_empty())
        .filter(|path| {
            path.components()
                .all(|part| matches!(part, Component::Normal(_)))
        });
    let name = relative
        .map(|path| path.as_os_str())
        .or_else(|| script.file_name())
        .unwrap_or_else(|| std::ffi::OsStr::new("index.php"));
    format!("/{}", name.to_string_lossy())
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn decode_path(path: &[u8]) -> Vec<u8> {
    let mut decoded = Vec::with_capacity(path.len());
    let mut cursor = 0;
    while cursor < path.len() {
        if path[cursor] == b'%' && cursor + 2 < path.len() {
            if let (Some(a), Some(b)) = (hex(path[cursor + 1]), hex(path[cursor + 2])) {
                decoded.push(a * 16 + b);
                cursor += 3;
                continue;
            }
        }
        decoded.push(path[cursor]);
        cursor += 1;
    }
    decoded
}
fn hex(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn configured_params(script_filename: &str, document_root: &str) -> Vec<(Vec<u8>, Vec<u8>)> {
        let config = Config {
            endpoint: "127.0.0.1:8080".into(),
            fpm_addr: "127.0.0.1:9000".into(),
            script_filename: script_filename.into(),
            document_root: document_root.into(),
            max_in_flight: 4,
            queue_capacity: 4,
            queue_timeout: std::time::Duration::from_secs(1),
            request_timeout: std::time::Duration::from_secs(30),
            max_body_bytes: 6_291_456,
            fpm_bin: "/opt/bin/php-fpm".into(),
            fpm_config: "/tmp/bref-web/php-fpm.conf".into(),
            shutdown_grace: std::time::Duration::from_millis(500),
        };
        params(
            &config,
            &Method::GET,
            &"/orders/a%2Fb?next=%2Fcheckout&tag=a+z"
                .parse::<Uri>()
                .unwrap(),
            &HeaderMap::new(),
            0,
            "fixture-front-controller",
        )
    }

    fn param<'a>(params: &'a [(Vec<u8>, Vec<u8>)], name: &str) -> &'a [u8] {
        &params
            .iter()
            .find(|(key, _)| key.as_slice() == name.as_bytes())
            .unwrap()
            .1
    }

    #[test]
    fn default_front_controller_keeps_index_script_name() {
        let values = configured_params("/var/task/public/index.php", "/var/task/public");
        assert_eq!(param(&values, "SCRIPT_NAME"), b"/index.php");
        assert_eq!(param(&values, "DOCUMENT_URI"), b"/index.php");
    }

    #[test]
    fn custom_front_controller_changes_script_metadata_only() {
        let values = configured_params("/var/task/public/app.php", "/var/task/public/");
        assert_eq!(param(&values, "SCRIPT_NAME"), b"/app.php");
        assert_eq!(param(&values, "DOCUMENT_URI"), b"/app.php");
        assert_eq!(
            param(&values, "SCRIPT_FILENAME"),
            b"/var/task/public/app.php"
        );
        assert_eq!(
            param(&values, "REQUEST_URI"),
            b"/orders/a%2Fb?next=%2Fcheckout&tag=a+z"
        );
        assert_eq!(param(&values, "QUERY_STRING"), b"next=%2Fcheckout&tag=a+z");
        assert_eq!(param(&values, "PATH_INFO"), b"/orders/a/b");
    }

    #[test]
    fn nested_front_controller_preserves_relative_public_path() {
        let values = configured_params("/var/task/public/admin/front.php", "/var/task/public");
        assert_eq!(param(&values, "SCRIPT_NAME"), b"/admin/front.php");
        assert_eq!(param(&values, "DOCUMENT_URI"), b"/admin/front.php");
    }

    #[test]
    fn outside_document_root_uses_basename_and_component_boundaries() {
        for script in [
            "/var/task/controllers/app.php",
            "/var/task/public-other/app.php",
            "/var/task/public/../app.php",
        ] {
            let values = configured_params(script, "/var/task/public");
            assert_eq!(param(&values, "SCRIPT_NAME"), b"/app.php");
            assert_eq!(param(&values, "DOCUMENT_URI"), b"/app.php");
            assert_eq!(param(&values, "SCRIPT_FILENAME"), script.as_bytes());
        }
    }

    #[test]
    fn preserves_duplicate_cookies_binary_header_and_body() {
        let h = parse_head(b"Status: 201 Created\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\nX-Raw: \xff\r\n\r\n\0\xff").unwrap().unwrap();
        assert_eq!(h.status, 201);
        assert_eq!(h.headers.get_all(header::SET_COOKIE).iter().count(), 2);
        assert_eq!(h.headers["x-raw"].as_bytes(), [255]);
        assert_eq!(h.remainder.as_ref(), [0, 255]);
    }

    #[test]
    fn incomplete_headers_limits_and_conflicting_length() {
        assert!(parse_head(b"Content-Type: text/plain\r\n")
            .unwrap()
            .is_none());
        assert!(parse_head(&vec![b'x'; MAX_HEADER_BYTES + 1]).is_err());
        assert!(parse_head(b"Content-Length: 1\r\nContent-Length: 2\r\n\r\n").is_err());
        assert_eq!(
            parse_head(b"Location: /next\r\n\r\n")
                .unwrap()
                .unwrap()
                .status,
            302
        );
    }

    #[test]
    fn strips_connection_nominated_headers() {
        let h = parse_head(
            b"Connection: keep-alive, X-Secret\r\nX-Secret: no\r\nSet-Cookie: yes\r\n\r\n",
        )
        .unwrap()
        .unwrap();
        assert!(!h.headers.contains_key("connection"));
        assert!(!h.headers.contains_key("x-secret"));
        assert!(h.headers.contains_key("set-cookie"));
    }
}
