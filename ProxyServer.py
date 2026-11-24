from socket import *
import sys
import os
import hashlib
import threading

if len(sys.argv) <= 1:
    print('Usage : "python ProxyServer.py server_ip [port]"\n'
          '[server_ip : It is the IP Address Of Proxy Server]')
    sys.exit(2)

SERVER_IP = sys.argv[1]
SERVER_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8888
CACHE_DIR = "cache"
TIMEOUT = 10  # seconds, for connecting to / reading from the remote server

os.makedirs(CACHE_DIR, exist_ok=True)

# In-memory index of what is cached: url key -> file path on disk
cache_index = {}
cache_lock = threading.Lock()


def send_error(sock, code, reason, text):
    """Send a simple HTTP error response to the client."""
    body = ("<html><body><h1>%d %s</h1><p>%s</p></body></html>"
            % (code, reason, text)).encode()
    head = ("HTTP/1.0 %d %s\r\nContent-Type: text/html\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n"
            % (code, reason, len(body))).encode()
    try:
        sock.sendall(head + body)
    except OSError:
        pass


def recv_request(sock):
    """Receive the full request (headers + body if Content-Length exists)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    head_text = head.decode("iso-8859-1")

    content_length = 0
    for line in head_text.split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
    while len(body) < content_length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return head_text, body


def parse_headers(head_text):
    headers = {}
    for line in head_text.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return headers


def parse_target(target, headers):
    """
    Return (host, port, path) or None if the URL is invalid/unsupported.
    Supports both styles:
      GET /www.example.com/page HTTP/1.1          (URL typed after proxy address)
      GET http://www.example.com/page HTTP/1.1    (browser configured to use proxy)
    """
    absolute = False
    if target.startswith("http://"):
        target = target[len("http://"):]
        absolute = True
    elif target.startswith("https://"):
        return None
    else:
        target = target.lstrip("/")

    # Relative links inside a proxied page (e.g. /img/logo.png): use Referer
    first = target.split("/", 1)[0]
    if not absolute and "." not in first and first != "localhost":
        ref = headers.get("referer", "")
        if "://" in ref:
            ref_path = ref.split("://", 1)[1].partition("/")[2]
            ref_host = ref_path.split("/", 1)[0]
            if ref_host:
                target = ref_host + "/" + target

    hostport, _, rest = target.partition("/")
    path = "/" + rest
    if not hostport:
        return None
    host, _, port = hostport.partition(":")
    try:
        port = int(port) if port else 80
    except ValueError:
        return None
    return host, port, path


def fetch_from_server(method, host, port, path, headers, body):
    """Connect to the origin server, send the request, return the raw response."""
    c = socket(AF_INET, SOCK_STREAM)                      # socket to the web server
    c.settimeout(TIMEOUT)
    try:
        c.connect((host, port))                            # connect to port 80
        host_header = host if port == 80 else "%s:%d" % (host, port)
        req = "%s %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n" % (
            method, path, host_header)
        req += "User-Agent: SimpleProxy/1.0\r\n"
        if method == "POST":
            if "content-type" in headers:
                req += "Content-Type: %s\r\n" % headers["content-type"]
            req += "Content-Length: %d\r\n" % len(body)
        req += "\r\n"
        c.sendall(req.encode("iso-8859-1") + body)

        response = b""                                     # read response into buffer
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            response += chunk
        return response
    finally:
        c.close()


def handle_client(tcpCliSock, addr):
    try:
        message, body = recv_request(tcpCliSock)
        print(message)
        if not message.strip():
            return

        parts = message.split("\r\n")[0].split()
        if len(parts) < 3:
            send_error(tcpCliSock, 400, "Bad Request", "Malformed request line.")
            return
        method, target = parts[0].upper(), parts[1]
        headers = parse_headers(message)

        if method not in ("GET", "POST"):
            send_error(tcpCliSock, 501, "Not Implemented",
                       "Only GET and POST are supported.")
            return

        parsed = parse_target(target, headers)
        if parsed is None:
            send_error(tcpCliSock, 400, "Bad Request",
                       "Invalid or unsupported URL (only http:// is supported).")
            return
        host, port, path = parsed
        filename = host + path          # e.g. example.com/index.html
        print("Requested:", filename)

        key = "%s:%d%s" % (host, port, path)
        cache_file = os.path.join(CACHE_DIR, hashlib.md5(key.encode()).hexdigest())

        # ---------- Cache lookup (GET only) ----------
        if method == "GET":
            with cache_lock:
                cached_path = cache_index.get(key)
            if cached_path and os.path.exists(cached_path):
                with open(cached_path, "rb") as f:
                    data = f.read()
                tcpCliSock.sendall(data)    # cache hit: send stored response
                print("Read from cache:", key)
                return

        # ---------- Cache miss: contact the origin server ----------
        print("Cache miss, fetching from", host)
        try:
            response = fetch_from_server(method, host, port, path, headers, body)
        except timeout:
            send_error(tcpCliSock, 504, "Gateway Timeout",
                       "The remote server did not respond in time.")
            return
        except gaierror:
            send_error(tcpCliSock, 502, "Bad Gateway",
                       "Could not resolve host '%s'. The URL may be invalid." % host)
            return
        except OSError as e:
            send_error(tcpCliSock, 502, "Bad Gateway",
                       "Remote server unreachable: %s" % e)
            return

        if not response:
            send_error(tcpCliSock, 502, "Bad Gateway",
                       "Remote server sent an empty response.")
            return

        # Send the response to the client
        tcpCliSock.sendall(response)

        # Save to cache only successful GET responses
        status = response.split(b"\r\n", 1)[0].split(b" ")
        if method == "GET" and len(status) > 1 and status[1] == b"200":
            tmpFile = open(cache_file, "wb")
            tmpFile.write(response)
            tmpFile.close()
            with cache_lock:
                cache_index[key] = cache_file
            print("Cached as", cache_file)

    except Exception as e:
        print("Error handling request:", e)
        send_error(tcpCliSock, 500, "Internal Server Error", "Proxy error.")
    finally:
        tcpCliSock.close()          # close the client socket


# Create a server socket, bind it to a port and start listening
tcpSerSock = socket(AF_INET, SOCK_STREAM)
tcpSerSock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
tcpSerSock.bind((SERVER_IP, SERVER_PORT))
tcpSerSock.listen(10)

try:
    while 1:
        print('Ready to serve...')
        tcpCliSock, addr = tcpSerSock.accept()
        print('Received a connection from:', addr)
        threading.Thread(target=handle_client, args=(tcpCliSock, addr),
                         daemon=True).start()
except KeyboardInterrupt:
    print("\nShutting down proxy...")
finally:
    tcpSerSock.close()              # close the server socket