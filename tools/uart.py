#!/usr/bin/env python3
r"""Agent-friendly serial console CLI for UART debugging.

Purpose: screen/minicom are interactive; an AI agent needs one-shot calls
that open the port, read a bounded amount of output, send exact bytes, and
wait for a prompt. Every subcommand here does exactly one job, prints what
came back on stdout, writes meta-information to stderr, and exits with a
status code the caller can branch on.

Examples
--------
    python tools/uart.py ports
    python tools/uart.py read   -p COM5 -t 5
    python tools/uart.py send   -p COM5 -t 2 --append crlf "help"
    python tools/uart.py expect -p COM5 --pattern "s12#" -t 30
    python tools/uart.py expect -p COM5 --pattern "Hit any key" --reply "" -t 60
    python tools/uart.py monitor -p COM5 --save console.log
    python tools/uart.py bridge  -p COM5 --host 0.0.0.0 --port 5555

Exit codes: 0 = ok, 1 = open/serial error, 2 = expect timeout.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - dependency guard
    sys.exit("pyserial is required: python -m pip install pyserial")


DEFAULT_BAUD = 115200

PARITY = {
    "N": serial.PARITY_NONE,
    "E": serial.PARITY_EVEN,
    "O": serial.PARITY_ODD,
    "M": serial.PARITY_MARK,
    "S": serial.PARITY_SPACE,
}

STOPBITS = {
    "1": serial.STOPBITS_ONE,
    "1.5": serial.STOPBITS_ONE_POINT_FIVE,
    "2": serial.STOPBITS_TWO,
}

SUFFIX = {"none": b"", "lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}


def build_payload(text: str, as_hex: bool, append: str) -> bytes:
    """Turn the CLI argument into the exact bytes to write."""
    if as_hex:
        cleaned = text.replace("0x", "").replace("0X", "").replace(",", " ")
        data = bytes.fromhex(cleaned)
    else:
        data = text.encode("utf-8")
    return data + SUFFIX[append]


def open_port(args: argparse.Namespace) -> serial.Serial:
    if not args.port:
        raise SystemExit("--port is required (run: uart.py ports)")

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.bytesize = int(args.bytesize)
    ser.parity = PARITY[args.parity.upper()]
    ser.stopbits = STOPBITS[str(args.stopbits)]
    ser.timeout = 0.05
    ser.write_timeout = 2.0
    ser.xonxoff = False
    ser.rtscts = False
    ser.dsrdtr = False
    ser.open()

    for name in ("dtr", "rts"):
        want = getattr(args, name)
        if want is None:
            continue
        try:
            setattr(ser, name, want)
        except (serial.SerialException, OSError) as exc:
            print(f"warning: cannot set {name}: {exc}", file=sys.stderr)

    return ser


def pump(
    ser: serial.Serial,
    seconds: float | None,
    save=None,
    echo: bool = True,
    encoding: str = "utf-8",
) -> bytes:
    """Read for `seconds` (None = until Ctrl+C) and return the raw bytes."""
    deadline = None if seconds is None else time.monotonic() + seconds
    chunks: list[bytes] = []

    while True:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            chunks.append(chunk)
            if save is not None:
                save.write(chunk)
                save.flush()
            if echo:
                sys.stdout.write(chunk.decode(encoding, "replace"))
                sys.stdout.flush()
        if deadline is not None and time.monotonic() >= deadline:
            break

    return b"".join(chunks)


def hexdump(data: bytes) -> None:
    for offset in range(0, len(data), 16):
        row = data[offset : offset + 16]
        hexpart = " ".join(f"{byte:02x}" for byte in row)
        text = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in row)
        print(f"{offset:08x}  {hexpart:<47}  {text}")


def cmd_ports() -> int:
    rows = sorted(list_ports.comports(), key=lambda p: p.device)
    if not rows:
        print("(no serial ports found)")
        return 0
    for port in rows:
        print(f"{port.device}\t{port.description}\t{port.hwid}")
    return 0


def cmd_read(ser: serial.Serial, args, save) -> int:
    data = pump(
        ser,
        args.time,
        save,
        echo=not (args.quiet or args.hex),
        encoding=args.encoding,
    )
    if args.hex and not args.quiet:
        hexdump(data)
    return 0


def cmd_send(ser: serial.Serial, args, save) -> int:
    payload = build_payload(args.text, args.hex_in, args.append)
    ser.write(payload)
    ser.flush()
    if not args.quiet:
        print(f"[sent {len(payload)}B] {payload!r}", file=sys.stderr)
    if args.time > 0:
        pump(
            ser,
            args.time,
            save,
            echo=not (args.quiet or args.hex),
            encoding=args.encoding,
        )
    return 0


def cmd_expect(ser: serial.Serial, args, save) -> int:
    """Wait for a pattern; optionally answer it, then keep reading briefly.

    This is what makes the boot window usable: start expect first, power the
    board on, and when "Hit any key" shows up the reply goes out inside the
    same open port instead of a second, too-late CLI invocation.
    """
    regex = re.compile(args.pattern) if args.regex else None
    buffer = ""
    deadline = time.monotonic() + args.time
    matched = False

    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            continue
        if save is not None:
            save.write(chunk)
            save.flush()
        text = chunk.decode(args.encoding, "replace")
        buffer += text
        if not args.quiet:
            sys.stdout.write(text)
            sys.stdout.flush()

        hit = regex.search(buffer) if regex else args.pattern in buffer
        if not hit or matched:
            continue

        matched = True
        if args.reply is None:
            return 0

        payload = build_payload(args.reply, False, args.reply_append)
        ser.write(payload)
        ser.flush()
        if not args.quiet:
            print(f"\n[expect] matched, sent {payload!r}", file=sys.stderr)
        if args.after <= 0:
            return 0
        deadline = time.monotonic() + args.after

    if matched:
        return 0

    print(
        f"\n[expect] timeout after {args.time:.1f}s: {args.pattern!r} not seen",
        file=sys.stderr,
    )
    return 2


def cmd_monitor(ser: serial.Serial, args, save) -> int:
    try:
        pump(ser, None, save, echo=not args.quiet, encoding=args.encoding)
    except KeyboardInterrupt:
        print()
    return 0


def relay(ser: serial.Serial, conn: socket.socket, save) -> None:
    """Pump bytes both ways until the TCP peer disconnects.

    Polling instead of select() because Windows cannot select() on a serial
    handle; 50 ms in each direction is fine for a console.
    """
    conn.settimeout(0.05)
    while True:
        incoming = ser.read(ser.in_waiting or 1)
        if incoming:
            if save is not None:
                save.write(incoming)
                save.flush()
            conn.sendall(incoming)
        try:
            outgoing = conn.recv(4096)
        except socket.timeout:
            continue
        if not outgoing:
            return
        ser.write(outgoing)
        ser.flush()


def cmd_bridge(ser: serial.Serial, args, save) -> int:
    """Serve the serial port over TCP so WSL, a container, or another host can use it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(1)
    print(
        f"[bridge] tcp {args.host}:{args.port} <-> {ser.port} @ {ser.baudrate}",
        file=sys.stderr,
    )
    try:
        while True:
            conn, peer = server.accept()
            print(f"[bridge] client {peer[0]}:{peer[1]}", file=sys.stderr)
            try:
                relay(ser, conn, save)
            except OSError as exc:
                print(f"[bridge] client error: {exc}", file=sys.stderr)
            finally:
                conn.close()
                print("[bridge] client closed", file=sys.stderr)
    finally:
        server.close()


def cmd_selftest() -> int:
    print(f"python   {sys.version.split()[0]}")
    print(f"platform {sys.platform}")
    print(f"pyserial {getattr(serial, '__version__', 'unknown')}")
    try:
        count = len(list_ports.comports())
        print(f"ports    {count}")
    except Exception as exc:  # noqa: BLE001 - selftest must not crash
        print(f"ports    error: {exc}")

    checks = [
        ("payload text", build_payload("help", False, "crlf") == b"help\r\n"),
        ("payload hex", build_payload("48 65 6c 6c 6f", True, "none") == b"Hello"),
        ("suffix none", build_payload("x", False, "none") == b"x"),
        ("bad hex raises", _raises_value_error("zz")),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"check {name:<14} {'ok' if ok else 'FAILED'}")
    print("selftest", "ok" if not failed else f"FAILED ({', '.join(failed)})")
    return 0 if not failed else 1


def _raises_value_error(text: str) -> bool:
    try:
        bytes.fromhex(text)
    except ValueError:
        return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uart.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--port", help="COM5, /dev/ttyUSB0, ...")
    common.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD)
    common.add_argument("--bytesize", default="8", choices=["5", "6", "7", "8"])
    common.add_argument("--parity", default="N", choices=["N", "E", "O", "M", "S"])
    common.add_argument("--stopbits", default="1", choices=["1", "1.5", "2"])
    common.add_argument(
        "-t",
        "--time",
        type=float,
        default=5.0,
        help="seconds to keep reading (expect: total wait, default 5)",
    )
    common.add_argument("--encoding", default="utf-8")
    common.add_argument("--hex", action="store_true", help="print received bytes as hex")
    common.add_argument("--save", help="append raw received bytes to this file")
    common.add_argument("--quiet", action="store_true", help="do not stream device output")
    common.add_argument(
        "--dtr",
        dest="dtr",
        action="store_const",
        const=True,
        default=None,
        help="drive DTR high after open",
    )
    common.add_argument("--no-dtr", dest="dtr", action="store_const", const=False)
    common.add_argument(
        "--rts",
        dest="rts",
        action="store_const",
        const=True,
        default=None,
        help="drive RTS high after open",
    )
    common.add_argument("--no-rts", dest="rts", action="store_const", const=False)

    sub.add_parser("ports", help="list serial ports")
    sub.add_parser("selftest", help="check pyserial and argument handling, no hardware")

    sub.add_parser("read", parents=[common], help="read for N seconds and print")

    p_send = sub.add_parser("send", parents=[common], help="write bytes, then read replies")
    p_send.add_argument("text", help="payload; literal text unless --hex-in")
    p_send.add_argument("--hex-in", action="store_true", help="payload is hex, e.g. '48 65'")
    p_send.add_argument(
        "--append",
        default="none",
        choices=["none", "lf", "cr", "crlf"],
        help="line ending appended to the payload",
    )

    p_expect = sub.add_parser(
        "expect", parents=[common], help="read until a pattern appears or timeout"
    )
    p_expect.add_argument("--pattern", required=True)
    p_expect.add_argument("--regex", action="store_true", help="treat pattern as regex")
    p_expect.add_argument("--reply", help="text to send once the pattern matches")
    p_expect.add_argument(
        "--reply-append",
        default="cr",
        choices=["none", "lf", "cr", "crlf"],
        help="line ending for --reply (default cr, what a bootloader expects)",
    )
    p_expect.add_argument(
        "--after",
        type=float,
        default=1.0,
        help="seconds to keep reading after --reply (default 1)",
    )

    sub.add_parser("monitor", parents=[common], help="stream until Ctrl+C")

    p_bridge = sub.add_parser(
        "bridge", parents=[common], help="serve the port over TCP (for WSL/containers)"
    )
    p_bridge.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 for WSL")
    p_bridge.add_argument("--port", type=int, default=5555, help="TCP port (default 5555)")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "ports":
        return cmd_ports()
    if args.cmd == "selftest":
        return cmd_selftest()

    save = open(args.save, "ab") if args.save else None
    try:
        ser = open_port(args)
        with ser:
            if args.cmd == "read":
                return cmd_read(ser, args, save)
            if args.cmd == "send":
                return cmd_send(ser, args, save)
            if args.cmd == "expect":
                return cmd_expect(ser, args, save)
            if args.cmd == "monitor":
                return cmd_monitor(ser, args, save)
            if args.cmd == "bridge":
                return cmd_bridge(ser, args, save)
        raise SystemExit(f"unknown command: {args.cmd}")
    except serial.SerialException as exc:
        print(f"serial error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"bad argument: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if save is not None:
            save.close()


if __name__ == "__main__":
    sys.exit(main())
