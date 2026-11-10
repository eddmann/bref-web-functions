//! Single-request FastCGI responder framing. No multiplexing or persistent connections.
//! Protocol shape follows the FastCGI specification; tests cover byte fragmentation and truncation.
use std::io::{self, ErrorKind};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const STDOUT: u8 = 6;
pub const STDERR: u8 = 7;
pub const END_REQUEST: u8 = 3;
const MAX_RECORD: usize = 65_535;

#[derive(Debug)]
pub struct Record {
    pub kind: u8,
    pub content: Vec<u8>,
}

pub async fn read_record<R: AsyncRead + Unpin>(reader: &mut R) -> io::Result<Record> {
    let mut header = [0u8; 8];
    reader.read_exact(&mut header).await?;
    if header[0] != 1 {
        return Err(invalid("unsupported FastCGI version"));
    }
    if u16::from_be_bytes([header[2], header[3]]) != 1 {
        return Err(invalid("unexpected FastCGI request ID"));
    }
    if !matches!(header[1], STDOUT | STDERR | END_REQUEST) {
        return Err(invalid("unexpected FastCGI record type"));
    }
    let length = u16::from_be_bytes([header[4], header[5]]) as usize;
    if header[1] == END_REQUEST && length != 8 {
        return Err(invalid("END_REQUEST body must be eight bytes"));
    }
    let mut content = vec![0u8; length];
    reader.read_exact(&mut content).await?;
    let mut padding = [0u8; 255];
    reader
        .read_exact(&mut padding[..header[6] as usize])
        .await?;
    Ok(Record {
        kind: header[1],
        content,
    })
}

pub fn end_status(record: &Record) -> io::Result<u32> {
    if record.kind != END_REQUEST || record.content.len() != 8 {
        return Err(invalid("invalid END_REQUEST"));
    }
    if record.content[4] != 0 {
        return Err(invalid("FastCGI protocol status was not REQUEST_COMPLETE"));
    }
    let status = &record.content;
    Ok(u32::from_be_bytes([
        status[0], status[1], status[2], status[3],
    ]))
}

async fn write_record<W: AsyncWrite + Unpin>(
    writer: &mut W,
    kind: u8,
    content: &[u8],
) -> io::Result<()> {
    let length = u16::try_from(content.len()).map_err(|_| invalid("record too large"))?;
    let [high, low] = length.to_be_bytes();
    writer.write_all(&[1, kind, 0, 1, high, low, 0, 0]).await?;
    writer.write_all(content).await
}

pub async fn write_request<W: AsyncWrite + Unpin>(
    writer: &mut W,
    params: &[(Vec<u8>, Vec<u8>)],
    body: &[u8],
) -> io::Result<()> {
    write_record(writer, 1, &[0, 1, 0, 0, 0, 0, 0, 0]).await?;
    let mut encoded = Vec::new();
    for (name, value) in params {
        let mut pair = Vec::new();
        encode_length(name.len(), &mut pair)?;
        encode_length(value.len(), &mut pair)?;
        pair.extend_from_slice(name);
        pair.extend_from_slice(value);
        if pair.len() > MAX_RECORD {
            return Err(invalid("individual FastCGI parameter exceeds record size"));
        }
        if pair.len() > MAX_RECORD - encoded.len() {
            write_record(writer, 4, &encoded).await?;
            encoded.clear();
        }
        encoded.extend_from_slice(&pair);
    }
    if !encoded.is_empty() {
        write_record(writer, 4, &encoded).await?;
    }
    write_record(writer, 4, &[]).await?;
    for chunk in body.chunks(MAX_RECORD) {
        write_record(writer, 5, chunk).await?;
    }
    write_record(writer, 5, &[]).await?;
    writer.flush().await
}

fn encode_length(length: usize, output: &mut Vec<u8>) -> io::Result<()> {
    if length < 128 {
        output.push(length as u8);
    } else {
        let n = u32::try_from(length).map_err(|_| invalid("parameter too large"))?;
        if n > 0x7fff_ffff {
            return Err(invalid("parameter too large"));
        }
        output.extend_from_slice(&(n | 0x8000_0000).to_be_bytes());
    }
    Ok(())
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fragmented_record_padding_and_end_status() {
        let (mut tx, mut rx) = tokio::io::duplex(1);
        let producer = tokio::spawn(async move {
            tx.write_all(&[1, 6, 0, 1, 0, 3, 2, 0, 0, 255, 1, 0, 0])
                .await
                .unwrap();
            write_record(&mut tx, 3, &[0, 0, 0, 7, 0, 0, 0, 0])
                .await
                .unwrap();
        });
        assert_eq!(read_record(&mut rx).await.unwrap().content, [0, 255, 1]);
        assert_eq!(end_status(&read_record(&mut rx).await.unwrap()).unwrap(), 7);
        producer.await.unwrap();
    }

    #[tokio::test]
    async fn rejects_truncation_bad_header_and_short_end() {
        for data in [
            vec![1, 6, 0],
            vec![2, 6, 0, 1, 0, 0, 0, 0],
            vec![1, 6, 0, 2, 0, 0, 0, 0],
            vec![1, 3, 0, 1, 0, 1, 0, 0, 0],
            vec![1, 6, 0, 1, 0, 3, 0, 0, 1],
        ] {
            assert!(read_record(&mut data.as_slice()).await.is_err());
        }
    }

    #[tokio::test]
    async fn splits_large_binary_input_and_terminates_streams() {
        let body = vec![0xff; 130_000];
        let mut encoded = Vec::new();
        write_request(&mut encoded, &[(b"LONG".to_vec(), vec![b'a'; 200])], &body)
            .await
            .unwrap();
        let mut offset = 0;
        let mut observed_body = Vec::new();
        let mut params_end = 0;
        let mut body_end = 0;
        while offset < encoded.len() {
            let kind = encoded[offset + 1];
            let length = u16::from_be_bytes([encoded[offset + 4], encoded[offset + 5]]) as usize;
            if kind == 4 && length == 0 {
                params_end += 1;
            }
            if kind == 5 && length == 0 {
                body_end += 1;
            }
            if kind == 5 {
                observed_body.extend_from_slice(&encoded[offset + 8..offset + 8 + length]);
            }
            offset += 8 + length;
        }
        assert_eq!(observed_body, body);
        assert_eq!((params_end, body_end), (1, 1));
    }

    #[tokio::test]
    async fn parameter_pairs_do_not_cross_record_boundaries() {
        let mut encoded = Vec::new();
        write_request(
            &mut encoded,
            &[
                (b"FIRST".to_vec(), vec![b'a'; 42_000]),
                (b"SECOND".to_vec(), vec![b'b'; 42_000]),
            ],
            &[],
        )
        .await
        .unwrap();
        let mut offset = 0;
        let mut parameters = Vec::new();
        while offset < encoded.len() {
            let kind = encoded[offset + 1];
            let length = u16::from_be_bytes([encoded[offset + 4], encoded[offset + 5]]) as usize;
            if kind == 4 && length > 0 {
                parameters.push(encoded[offset + 8..offset + 8 + length].to_vec());
            }
            offset += 8 + length;
        }
        assert_eq!(parameters.len(), 2);
        assert!(parameters[0].windows(5).any(|w| w == b"FIRST"));
        assert!(parameters[1].windows(6).any(|w| w == b"SECOND"));
        assert_eq!(parameters[0].len(), 42_010);
        assert_eq!(parameters[1].len(), 42_011);
        assert!(
            write_request(&mut Vec::new(), &[(b"X".to_vec(), vec![0; 65_535])], &[])
                .await
                .is_err()
        );
    }
}
