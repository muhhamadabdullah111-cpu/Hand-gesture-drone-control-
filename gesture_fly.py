import math
import sys
import time
import socket
import subprocess
import platform
import threading
import urllib.request
import urllib.error
import cv2

# ============================================================
# MediaPipe
# ============================================================

try:
    import mediapipe as mp

    if hasattr(mp, "solutions"):
        mp_hands = mp.solutions.hands
        mp_drawing = mp.solutions.drawing_utils
    else:
        raise ImportError("mediapipe.solutions not available")

except Exception as error:
    print("MediaPipe import failed:", error)
    sys.exit(1)


# ============================================================
# DRONE CONFIGURATION
# ============================================================

DRONE_IP = "udp://192.168.43.43"

# TCP ports to test (HTTP / WebSocket style firmware)
TCP_PORTS_TO_SCAN = [
    80, 81, 8080, 8000, 5000, 3000, 8888, 8081, 8008, 8088, 9000,
    23, 21, 1234, 4210, 8090, 2000, 2001,
]

# UDP ports to test. 8889 is the Tello / Tello-clone AT-command port,
# which is what a LOT of ESP32 "drone" firmwares copy. 2390/8890 are
# used by some Tello-EDU / clone variants. If your firmware is custom,
# check the .ino / firmware source for the real port instead of guessing.
UDP_PORTS_TO_SCAN = [8889, 8890, 2390, 8886, 1000]

CONTROL_ENDPOINT = "/control"

TCP_CONNECT_TIMEOUT = 0.35   # short timeout so a full scan is fast
UDP_TIMEOUT = 1.0
HTTP_TIMEOUT = 2.0
COMMAND_DELAY = 0.5

DRONE_PORT = None
DRONE_URL = None
DRONE_PROTOCOL = None   # "tcp_http" or "udp"
UDP_SOCK = None


# ============================================================
# STEP 0: CONFIRM THE HOST IS ACTUALLY ALIVE
# ============================================================

def ping_host(ip):
    """
    A TCP port scan tells you nothing if the host itself is
    unreachable. Ping first so you know whether the problem is
    'wrong port/protocol' vs 'not actually connected to this IP'.
    """
    print()
    print("========================================")
    print("       CHECKING HOST IS ALIVE (PING)")
    print("========================================")

    count_flag = "-n" if platform.system().lower() == "windows" else "-c"

    try:
        result = subprocess.run(
            ["ping", count_flag, "2", ip],
            capture_output=True,
            text=True,
            timeout=5,
        )
        alive = result.returncode == 0
        print(f"Ping {'succeeded' if alive else 'FAILED'} for {ip}")
        if not alive:
            print(result.stdout)
            print(
                "If this fails: you are on the wrong IP/subnet, or the "
                "device is not actually connected. Fix that before "
                "worrying about ports at all — nothing below will work."
            )
        return alive
    except Exception as error:
        print(f"Could not run ping: {error}")
        return False


# ============================================================
# STEP 1: FAST THREADED TCP PORT SCAN
# ============================================================

def _check_tcp_port(ip, port, open_ports, lock):
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TCP_CONNECT_TIMEOUT)
        if sock.connect_ex((ip, port)) == 0:
            with lock:
                open_ports.append(port)
    except Exception:
        pass
    finally:
        if sock:
            sock.close()


def scan_tcp_ports(ip, ports):
    print()
    print("========================================")
    print("       SCANNING TCP PORTS")
    print("========================================")
    print(f"Drone IP: {ip}")
    print(f"Ports:    {ports}")
    print()

    open_ports = []
    lock = threading.Lock()
    threads = []

    for port in ports:
        t = threading.Thread(
            target=_check_tcp_port, args=(ip, port, open_ports, lock)
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    open_ports.sort()

    if open_ports:
        print(f"✅ OPEN TCP PORTS: {open_ports}")
    else:
        print("❌ No open TCP ports found in the scanned list.")

    return open_ports


# ============================================================
# STEP 2: UDP PROBE
# ============================================================

def probe_udp_port(ip, port, message=b"command"):
    """
    UDP is connectionless, so there is no 'open/closed' handshake
    like TCP. We send a real command byte string and wait briefly
    for ANY reply. A reply (even an error string) means something
    is listening and talking back.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(UDP_TIMEOUT)
    try:
        sock.sendto(message, (ip, port))
        data, _ = sock.recvfrom(1024)
        print(f"✅ UDP {port} replied: {data!r}")
        return True
    except socket.timeout:
        print(f"❌ UDP {port} — no reply")
        return False
    except Exception as error:
        print(f"❌ UDP {port} — error: {error}")
        return False
    finally:
        sock.close()


def scan_udp_ports(ip, ports):
    print()
    print("========================================")
    print("       PROBING UDP PORTS")
    print("========================================")
    print(f"Drone IP: {ip}")
    print(f"Ports:    {ports}")
    print()

    responsive = []
    for port in ports:
        if probe_udp_port(ip, port):
            responsive.append(port)

    return responsive


# ============================================================
# STEP 3: TEST HTTP SERVER / CONTROL ENDPOINT (TCP candidates)
# ============================================================

def check_http_server(url):
    print()
    print("========================================")
    print("       TESTING HTTP SERVER")
    print("========================================")
    print(f"URL: {url}")

    try:
        response = urllib.request.urlopen(url, timeout=HTTP_TIMEOUT)
        data = response.read(500).decode("utf-8", errors="ignore")
        print(f"✅ HTTP SERVER RESPONDED — status {response.status}")
        if data:
            print("Response:", data)
        return True

    except urllib.error.HTTPError as error:
        # 404/405 still proves an HTTP server exists
        print(f"✅ HTTP SERVER REACHED — status {error.code}")
        return True

    except urllib.error.URLError as error:
        print(f"❌ HTTP SERVER NOT REACHABLE — {error}")
        return False

    except Exception as error:
        print(f"❌ HTTP TEST ERROR — {error}")
        return False


def test_control_endpoint(url, endpoint):
    full_url = f"{url}{endpoint}?cmd=stop"
    print()
    print("========================================")
    print("       TESTING CONTROL ENDPOINT")
    print("========================================")
    print(f"URL: {full_url}")

    try:
        response = urllib.request.urlopen(full_url, timeout=HTTP_TIMEOUT)
        data = response.read(500).decode("utf-8", errors="ignore")
        print(f"✅ CONTROL ENDPOINT RESPONDED — status {response.status}")
        if data:
            print("Response:", data)
        return True

    except urllib.error.HTTPError as error:
        print(f"⚠ SERVER REACHED — status {error.code}")
        if error.code == 404:
            print(f"❌ {endpoint} does not exist on this server.")
        elif error.code == 405:
            print("⚠ Endpoint exists but method/verb may be different.")
        return False

    except urllib.error.URLError as error:
        print(f"❌ CONTROL ENDPOINT FAILED — {error}")
        return False

    except Exception as error:
        print(f"❌ CONTROL TEST ERROR — {error}")
        return False


# ============================================================
# FULL DISCOVERY SEQUENCE
# ============================================================

def discover_drone():
    global DRONE_PORT, DRONE_URL, DRONE_PROTOCOL, UDP_SOCK

    if not ping_host(DRONE_IP):
        print(
            "\nHost did not respond to ping. Double-check DRONE_IP, that "
            "your laptop is on the SAME wifi network as the drone, and "
            "that the drone/ESP32 has actually booted and joined the "
            "network (check its serial monitor output for its assigned IP)."
        )
        return

    open_tcp = scan_tcp_ports(DRONE_IP, TCP_PORTS_TO_SCAN)

    for port in open_tcp:
        candidate_url = f"http://{DRONE_IP}:{port}"
        if check_http_server(candidate_url):
            DRONE_PORT = port
            DRONE_URL = candidate_url
            DRONE_PROTOCOL = "tcp_http"
            test_control_endpoint(candidate_url, CONTROL_ENDPOINT)
            print(f"\n✅ Using TCP/HTTP on port {port}")
            return

    # No usable TCP/HTTP server — try UDP
    open_udp = scan_udp_ports(DRONE_IP, UDP_PORTS_TO_SCAN)

    if open_udp:
        DRONE_PORT = open_udp[0]
        DRONE_PROTOCOL = "udp"
        UDP_SOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        UDP_SOCK.settimeout(UDP_TIMEOUT)
        print(f"\n✅ Using UDP on port {DRONE_PORT}")
        return

    print()
    print("========================================")
    print("       NO PROTOCOL FOUND")
    print("========================================")
    print(
        "Ping succeeded but nothing answered on TCP or the UDP ports "
        "tried. This almost always means the firmware uses a port or "
        "protocol not in these lists. Open the ESP32/drone firmware "
        "source code (the .ino file or equivalent) and search for "
        "'.begin(' / 'listen' / 'Serial.println' near WiFi setup — it "
        "will print or hard-code the exact port and protocol it uses. "
        "Update TCP_PORTS_TO_SCAN or UDP_PORTS_TO_SCAN with that value "
        "and rerun."
    )


# ============================================================
# SEND DRONE COMMAND (works for both protocols)
# ============================================================

def send_drone_command(command):
    if DRONE_PROTOCOL == "tcp_http" and DRONE_URL:
        url = f"{DRONE_URL}{CONTROL_ENDPOINT}?cmd={command}"
        try:
            response = urllib.request.urlopen(url, timeout=HTTP_TIMEOUT)
            data = response.read(300).decode("utf-8", errors="ignore")
            print(f"[SUCCESS] {command} | HTTP {response.status}")
            if data:
                print(f"[DRONE] {data}")
            return True
        except urllib.error.HTTPError as error:
            print(f"[HTTP ERROR] {command} | Status: {error.code}")
            return False
        except urllib.error.URLError as error:
            print(f"[NETWORK ERROR] {command}: {error}")
            return False
        except Exception as error:
            print(f"[ERROR] {command}: {error}")
            return False

    elif DRONE_PROTOCOL == "udp" and UDP_SOCK and DRONE_PORT:
        try:
            UDP_SOCK.sendto(command.encode(), (DRONE_IP, DRONE_PORT))
            print(f"[SENT UDP] {command}")
            try:
                data, _ = UDP_SOCK.recvfrom(1024)
                print(f"[DRONE REPLY] {data!r}")
            except socket.timeout:
                pass
            return True
        except Exception as error:
            print(f"[UDP ERROR] {command}: {error}")
            return False

    else:
        print(f"[NOT SENT] No working connection. Command: {command}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    discover_drone()

    if DRONE_PROTOCOL is None:
        print()
        print(
            "Camera will still start so you can keep developing gesture "
            "detection, but drone commands cannot be sent until the "
            "correct protocol/port is identified (see message above)."
        )

    print()
    print("Starting camera...")
    print("Press 'q' to quit.")
    print()

    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

    if not cap.isOpened():
        print("Unable to open camera.")
        return

    hands = mp_hands.Hands(
        static_image_mode=False,
        max_num_hands=1,
        model_complexity=1,
        min_detection_confidence=0.65,
        min_tracking_confidence=0.65,
    )

    last_gesture = None
    prev_middle_x = None
    prev_middle_y = None
    MOVEMENT_THRESHOLD = 30
    last_command_time = 0

    while True:
        success, frame = cap.read()
        if not success:
            time.sleep(0.01)
            continue

        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False
        results = hands.process(rgb)
        rgb.flags.writeable = True

        gesture = "NO HAND"
        command_to_send = None

        if results.multi_hand_landmarks:
            hand_landmarks = results.multi_hand_landmarks[0]
            mp_drawing.draw_landmarks(
                frame, hand_landmarks, mp_hands.HAND_CONNECTIONS
            )

            height, width, _ = frame.shape

            thumb = hand_landmarks.landmark[4]
            index = hand_landmarks.landmark[8]
            wrist = hand_landmarks.landmark[0]
            middle_mcp = hand_landmarks.landmark[9]

            thumb_x, thumb_y = int(thumb.x * width), int(thumb.y * height)
            index_x, index_y = int(index.x * width), int(index.y * height)
            wrist_x, wrist_y = int(wrist.x * width), int(wrist.y * height)
            middle_x = int(middle_mcp.x * width)
            middle_y = int(middle_mcp.y * height)

            thumb_index_distance = math.hypot(
                index_x - thumb_x, index_y - thumb_y
            )
            hand_size = math.hypot(middle_x - wrist_x, middle_y - wrist_y)
            if hand_size < 1:
                hand_size = 1

            normalized_distance = thumb_index_distance / hand_size

            if normalized_distance < 0.25:
                gesture = "PINCH (STOP)"
                command_to_send = "stop"
            else:
                gesture = "OPEN HAND"

                if prev_middle_x is not None and prev_middle_y is not None:
                    dx = middle_x - prev_middle_x
                    dy = middle_y - prev_middle_y

                    if abs(dx) > MOVEMENT_THRESHOLD or abs(dy) > MOVEMENT_THRESHOLD:
                        if abs(dx) > abs(dy):
                            if dx > 0:
                                command_to_send = "right"
                                gesture = "MOVING RIGHT"
                            else:
                                command_to_send = "left"
                                gesture = "MOVING LEFT"
                        else:
                            if dy < 0:
                                command_to_send = "up"
                                gesture = "MOVING UP"
                            else:
                                command_to_send = "down"
                                gesture = "MOVING DOWN"

                        prev_middle_x = middle_x
                        prev_middle_y = middle_y
                else:
                    prev_middle_x = middle_x
                    prev_middle_y = middle_y

            if command_to_send is None:
                prev_middle_x = middle_x
                prev_middle_y = middle_y

            cv2.circle(frame, (thumb_x, thumb_y), 10, (0, 200, 255), -1)
            cv2.circle(frame, (index_x, index_y), 10, (0, 200, 255), -1)
            cv2.line(frame, (thumb_x, thumb_y), (index_x, index_y), (255, 255, 255), 3)

        else:
            prev_middle_x = None
            prev_middle_y = None

        if gesture != last_gesture:
            print(f"Gesture: {gesture}")
            last_gesture = gesture

        current_time = time.time()
        if command_to_send and (current_time - last_command_time) > COMMAND_DELAY:
            send_drone_command(command_to_send)
            last_command_time = current_time

        cv2.putText(
            frame, gesture, (20, 45), cv2.FONT_HERSHEY_SIMPLEX,
            1, (0, 200, 255), 2, cv2.LINE_AA,
        )

        if DRONE_PORT:
            connection_text = f"Drone: {DRONE_IP}:{DRONE_PORT} ({DRONE_PROTOCOL})"
        else:
            connection_text = f"Drone: {DRONE_IP}:NO CONNECTION"

        cv2.putText(
            frame, connection_text, (20, 80), cv2.FONT_HERSHEY_SIMPLEX,
            0.6, (255, 255, 255), 2, cv2.LINE_AA,
        )

        cv2.imshow("Hand Gesture", frame)

        if (cv2.waitKey(1) & 0xFF) == ord("q"):
            break

    hands.close()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()