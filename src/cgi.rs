use crate::config::{Config, ForwardedHeaders};
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

/// Parses the CGI head at the start of `buffer`; `Ok(None)` until it is complete.
/// httparse ends the head at the first blank line (CRLF or bare LF).
pub fn parse_head(buffer: &[u8]) -> io::Result<Option<Head>> {
    let mut slots = [httparse::EMPTY_HEADER; MAX_HEADERS];
    let limit = buffer.len().min(MAX_HEADER_BYTES);
    let (consumed, fields) = match httparse::parse_headers(&buffer[..limit], &mut slots) {
        Ok(httparse::Status::Complete(head)) => head,
        Ok(httparse::Status::Partial) if buffer.len() > MAX_HEADER_BYTES => {
            return Err(invalid("CGI response headers too large"))
        }
        Ok(httparse::Status::Partial) => return Ok(None),
        Err(_) => return Err(invalid("malformed CGI response headers")),
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
        return Err(invalid("informational CGI responses are not supported"));
    }
    strip_hop_by_hop(&mut headers);
    headers.remove("x-accel-buffering");
    Ok(Some(Head {
        status,
        headers,
        remainder: Bytes::copy_from_slice(&buffer[consumed..]),
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
    add("SERVER_SOFTWARE", b"bref-web");
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
    // Bref takes SERVER_PORT from X-Forwarded-Port too. Without that header,
    // keep 443 rather than Bref's 80: this ingress is always HTTPS (HTTPS=on),
    // and https with port 80 would produce wrong absolute URLs.
    let server_port = if headers.contains_key("x-forwarded-port") {
        bref_port(headers)
    } else {
        "443".into()
    };
    add("SERVER_PORT", server_port.as_bytes());
    // Same as Bref's FpmHandler: the ingress is a loopback-like local hop, and
    // Bref's documented `trusted_proxies: '127.0.0.1'` relies on this value
    // to trust X-Forwarded-* headers. REMOTE_PORT follows X-Forwarded-Port.
    add("REMOTE_ADDR", b"127.0.0.1");
    add("REMOTE_PORT", bref_port(headers).as_bytes());
    add("SERVER_ADDR", b"127.0.0.1");
    add("LAMBDA_REQUEST_ID", request_id.as_bytes());
    if let Some(host) = headers.get(header::HOST) {
        add("SERVER_NAME", server_name(host.as_bytes()));
        add("HTTP_HOST", host.as_bytes());
    }
    let mut clean = headers.clone();
    strip_hop_by_hop(&mut clean);
    if config.forwarded_headers == ForwardedHeaders::Ingress {
        keep_ingress_headers(&mut clean);
    }
    if let Some(trace) = clean.get("x-amzn-trace-id") {
        // Bref exposes the X-Ray trace as _X_AMZN_TRACE_ID for SDKs to read.
        add("_X_AMZN_TRACE_ID", trace.as_bytes());
    }
    // As Bref's HttpRequestEvent: a body without Content-Type is form data, and
    // the content headers are also visible as HTTP_* values (as with nginx).
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .map(HeaderValue::as_bytes)
        .or((length > 0).then_some(b"application/x-www-form-urlencoded".as_slice()));
    if let Some(content_type) = content_type {
        add("CONTENT_TYPE", content_type);
        add("HTTP_CONTENT_TYPE", content_type);
    }
    if length > 0 || headers.contains_key(header::CONTENT_LENGTH) {
        add("HTTP_CONTENT_LENGTH", length.to_string().as_bytes());
    }
    for name in clean.keys() {
        // `X_Foo` and `X-Foo` would both become HTTP_X_FOO, and PHP keeps the
        // last duplicate. Drop underscore names (as nginx does by default) so a
        // header an upstream proxy does not recognise cannot shadow one it set.
        if name.as_str().contains('_') {
            continue;
        }
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

/// `BREF_WEB_FORWARDED_HEADERS=ingress`. Lambda's ingress overwrites
/// X-Forwarded-Proto and -Port and appends the address it saw to
/// X-Forwarded-For; it passes the other forwarding and trace headers through
/// from the client. Keep only what the ingress controls, so an application that
/// trusts every proxy (as the Laravel bridge does) cannot be given a client's IP,
/// host or URL prefix.
fn keep_ingress_headers(headers: &mut HeaderMap) {
    for name in [
        "x-forwarded-host",
        "x-forwarded-prefix",
        "x-forwarded-server",
        "forwarded",
        "x-real-ip",
        "x-amzn-trace-id",
    ] {
        headers.remove(name);
    }
    let ingress_address = headers
        .get_all("x-forwarded-for")
        .iter()
        .flat_map(|value| value.as_bytes().split(|&byte| byte == b','))
        .map(<[u8]>::trim_ascii)
        .rfind(|entry| !entry.is_empty())
        .and_then(|entry| HeaderValue::from_bytes(entry).ok());
    match ingress_address {
        Some(address) => {
            headers.insert("x-forwarded-for", address);
        }
        None => {
            headers.remove("x-forwarded-for");
        }
    }
}

/// Bref's `(int) ($headers['x-forwarded-port'][0] ?? 80)`: leading digits of
/// the first value, 0 when it has none, 80 when the header is absent.
fn bref_port(headers: &HeaderMap) -> String {
    let Some(value) = headers.get("x-forwarded-port") else {
        return "80".into();
    };
    let digits: String = value
        .as_bytes()
        .iter()
        .skip_while(|b| b.is_ascii_whitespace())
        .take_while(|b| b.is_ascii_digit())
        .map(|&b| b as char)
        .collect();
    digits
        .parse::<u64>()
        .map_or_else(|_| "0".into(), |port| port.to_string())
}

/// CGI SERVER_NAME is the host without a port; IPv6 literals lose brackets.
fn server_name(host: &[u8]) -> &[u8] {
    if let Some(rest) = host.strip_prefix(b"[") {
        return rest
            .iter()
            .position(|&b| b == b']')
            .map_or(host, |end| &rest[..end]);
    }
    match host.iter().rposition(|&b| b == b':') {
        Some(colon) if host[colon + 1..].iter().all(u8::is_ascii_digit) => &host[..colon],
        _ => host,
    }
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
            script_filename: script_filename.into(),
            document_root: document_root.into(),
            ..Config::for_tests()
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

    fn request_params(headers: &[(&'static str, &'static str)]) -> Vec<(Vec<u8>, Vec<u8>)> {
        params_with_length(headers, 0)
    }

    fn params_with_length(
        headers: &[(&'static str, &'static str)],
        length: usize,
    ) -> Vec<(Vec<u8>, Vec<u8>)> {
        params_in(ForwardedHeaders::Passthrough, headers, length)
    }

    fn params_in(
        forwarded_headers: ForwardedHeaders,
        headers: &[(&'static str, &'static str)],
        length: usize,
    ) -> Vec<(Vec<u8>, Vec<u8>)> {
        let mut map = HeaderMap::new();
        for (name, value) in headers {
            map.append(
                HeaderName::from_static(name),
                HeaderValue::from_static(value),
            );
        }
        let config = Config {
            forwarded_headers,
            ..Config::for_tests()
        };
        params(
            &config,
            &Method::GET,
            &"/".parse::<Uri>().unwrap(),
            &map,
            length,
            "id",
        )
    }

    fn all<'a>(params: &'a [(Vec<u8>, Vec<u8>)], name: &str) -> Vec<&'a [u8]> {
        params
            .iter()
            .filter(|(key, _)| key.as_slice() == name.as_bytes())
            .map(|(_, value)| value.as_slice())
            .collect()
    }

    const SPOOFING_CLIENT: &[(&str, &str)] = &[
        // What Lambda's ingress delivers for a client sending its own values.
        ("host", "abc.lambda-web.us-west-1.on.aws"),
        ("x-forwarded-for", "192.0.2.1, 203.0.113.7"),
        ("x-forwarded-proto", "https"),
        ("x-forwarded-port", "443"),
        ("x-forwarded-host", "evil.example"),
        ("x-forwarded-prefix", "/evil"),
        ("x-forwarded-server", "evil.example"),
        ("forwarded", "for=192.0.2.9;host=evil.example;proto=http"),
        ("x-real-ip", "192.0.2.5"),
        ("x-amzn-trace-id", "Root=spoofed"),
    ];

    #[test]
    fn passthrough_keeps_forwarding_headers_as_bref_does() {
        let values = params_in(ForwardedHeaders::Passthrough, SPOOFING_CLIENT, 0);
        assert_eq!(
            param(&values, "HTTP_X_FORWARDED_FOR"),
            b"192.0.2.1, 203.0.113.7"
        );
        assert_eq!(param(&values, "HTTP_X_FORWARDED_HOST"), b"evil.example");
        assert_eq!(param(&values, "_X_AMZN_TRACE_ID"), b"Root=spoofed");
    }

    #[test]
    fn ingress_mode_keeps_only_what_lambda_ingress_controls() {
        let values = params_in(ForwardedHeaders::Ingress, SPOOFING_CLIENT, 0);
        assert_eq!(all(&values, "HTTP_X_FORWARDED_FOR"), [b"203.0.113.7"]);
        assert_eq!(param(&values, "HTTP_X_FORWARDED_PROTO"), b"https");
        assert_eq!(param(&values, "HTTP_X_FORWARDED_PORT"), b"443");
        assert_eq!(param(&values, "SERVER_PORT"), b"443");
        assert_eq!(
            param(&values, "HTTP_HOST"),
            b"abc.lambda-web.us-west-1.on.aws"
        );
        for dropped in [
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PREFIX",
            "HTTP_X_FORWARDED_SERVER",
            "HTTP_FORWARDED",
            "HTTP_X_REAL_IP",
            "HTTP_X_AMZN_TRACE_ID",
            "_X_AMZN_TRACE_ID",
        ] {
            assert!(all(&values, dropped).is_empty(), "{dropped} reached PHP");
        }
    }

    #[test]
    fn ingress_mode_takes_the_last_entry_across_repeated_headers() {
        let repeated = params_in(
            ForwardedHeaders::Ingress,
            &[
                ("x-forwarded-for", "192.0.2.1"),
                ("x-forwarded-for", "198.51.100.2, 203.0.113.7 ,"),
            ],
            0,
        );
        assert_eq!(all(&repeated, "HTTP_X_FORWARDED_FOR"), [b"203.0.113.7"]);
        let empty = params_in(ForwardedHeaders::Ingress, &[("x-forwarded-for", " , ")], 0);
        assert!(all(&empty, "HTTP_X_FORWARDED_FOR").is_empty());
    }

    #[test]
    fn underscore_header_cannot_shadow_hyphenated_header() {
        let values = request_params(&[
            ("x-forwarded-for", "203.0.113.7"),
            ("x_forwarded_for", "198.51.100.1"),
            ("x_only_underscore", "dropped"),
        ]);
        assert_eq!(all(&values, "HTTP_X_FORWARDED_FOR"), [b"203.0.113.7"]);
        assert!(all(&values, "HTTP_X_ONLY_UNDERSCORE").is_empty());
        let mut names: Vec<_> = values.iter().map(|(key, _)| key).collect();
        let count = names.len();
        names.sort();
        names.dedup();
        assert_eq!(names.len(), count, "every CGI parameter name is unique");
    }

    #[test]
    fn server_port_follows_forwarded_port_with_https_default() {
        assert_eq!(param(&request_params(&[]), "SERVER_PORT"), b"443");
        let values = request_params(&[("x-forwarded-port", "8443")]);
        assert_eq!(param(&values, "SERVER_PORT"), b"8443");
    }

    #[test]
    fn content_headers_and_trace_follow_bref() {
        let values = request_params(&[("x-amzn-trace-id", "Root=1-abc;Sampled=1")]);
        assert_eq!(param(&values, "_X_AMZN_TRACE_ID"), b"Root=1-abc;Sampled=1");
        assert!(all(&values, "CONTENT_TYPE").is_empty());
        assert!(all(&values, "HTTP_CONTENT_TYPE").is_empty());
        assert!(all(&values, "HTTP_CONTENT_LENGTH").is_empty());

        let with_body = params_with_length(&[], 13);
        assert_eq!(
            param(&with_body, "CONTENT_TYPE"),
            b"application/x-www-form-urlencoded"
        );
        assert_eq!(
            param(&with_body, "HTTP_CONTENT_TYPE"),
            b"application/x-www-form-urlencoded"
        );
        assert_eq!(param(&with_body, "HTTP_CONTENT_LENGTH"), b"13");

        let json = params_with_length(&[("content-type", "application/json")], 2);
        assert_eq!(param(&json, "CONTENT_TYPE"), b"application/json");
        assert_eq!(param(&json, "HTTP_CONTENT_TYPE"), b"application/json");
        assert_eq!(all(&json, "CONTENT_TYPE").len(), 1);
    }

    #[test]
    fn addresses_follow_bref_fpm_handler() {
        let values = request_params(&[]);
        assert_eq!(param(&values, "REMOTE_ADDR"), b"127.0.0.1");
        assert_eq!(param(&values, "SERVER_ADDR"), b"127.0.0.1");
        assert_eq!(param(&values, "REMOTE_PORT"), b"80");
        for (forwarded, port) in [
            ("443", "443"),
            (" 8443, 80", "8443"),
            ("x", "0"),
            ("0080", "80"),
        ] {
            let values = request_params(&[("x-forwarded-port", forwarded)]);
            assert_eq!(
                param(&values, "REMOTE_PORT"),
                port.as_bytes(),
                "{forwarded:?}"
            );
        }
    }

    #[test]
    fn server_name_is_host_without_port() {
        for (host, name) in [
            ("example.com:8443", "example.com"),
            ("example.com", "example.com"),
            ("[2001:db8::1]:8443", "2001:db8::1"),
            ("[2001:db8::1]", "2001:db8::1"),
            ("example.com:", "example.com"),
            ("example.com:x", "example.com:x"),
        ] {
            let values = request_params(&[("host", host)]);
            assert_eq!(param(&values, "SERVER_NAME"), name.as_bytes(), "{host}");
            assert_eq!(param(&values, "HTTP_HOST"), host.as_bytes(), "{host}");
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
    fn body_starts_after_the_first_blank_line() {
        // An LF-terminated head is not extended to a CRLFCRLF inside the body.
        let lf = parse_head(b"Content-Type: text/plain\n\nline one\r\n\r\nline two")
            .unwrap()
            .unwrap();
        assert_eq!(lf.remainder.as_ref(), b"line one\r\n\r\nline two");
        // A response with no headers keeps the whole body.
        let empty = parse_head(b"\r\nbody\r\n\r\nmore").unwrap().unwrap();
        assert_eq!(empty.status, 200);
        assert_eq!(empty.remainder.as_ref(), b"body\r\n\r\nmore");
    }

    #[test]
    fn head_may_end_exactly_at_the_limit() {
        let filler = MAX_HEADER_BYTES - b"X: \r\n\r\n".len();
        let mut buffer = format!("X: {}\r\n\r\n", "a".repeat(filler)).into_bytes();
        buffer.extend_from_slice(b"body");
        assert_eq!(
            parse_head(&buffer).unwrap().unwrap().remainder.as_ref(),
            b"body"
        );
        buffer.insert(0, b'Y');
        assert!(parse_head(&buffer[..MAX_HEADER_BYTES]).unwrap().is_none());
        assert!(parse_head(&buffer).is_err());
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
