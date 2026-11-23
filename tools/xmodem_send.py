import sys
import time
import serial

PORT = 'COM5'
BAUD = 115200


def crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def wait_byte(ser, seconds):
    end = time.time() + seconds
    while time.time() < end:
        b = ser.read(1)
        if b:
            return b[0]
    return None


def main():
    path, off, length, addr = sys.argv[1], int(sys.argv[2], 0), int(sys.argv[3], 0), sys.argv[4]
    with open(path, 'rb') as f:
        f.seek(off)
        data = f.read(length)
    if len(data) != length:
        print('SHORT READ', len(data))
        return 2

    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    ser.reset_input_buffer()
    ser.write(b'\r')
    time.sleep(0.6)
    ser.reset_input_buffer()
    ser.write(('loadx %s\r' % addr).encode())

    got = None
    end = time.time() + 20
    while time.time() < end:
        b = ser.read(1)
        if not b:
            continue
        if b[0] in (0x43, 0x15):
            got = b[0]
            break
    if got is None:
        print('NO HANDSHAKE')
        return 3
    print('HANDSHAKE ok (0x%02x)' % got, flush=True)

    seq = 1
    nblocks = (len(data) + 127) // 128
    for i in range(nblocks):
        blk = data[i * 128:(i + 1) * 128]
        if len(blk) < 128:
            blk = blk + bytes([0x1A]) * (128 - len(blk))
        c = crc16(blk)
        pkt = bytes([0x01, seq & 0xFF, (~seq) & 0xFF]) + blk + bytes([(c >> 8) & 0xFF, c & 0xFF])
        acked = False
        for _ in range(12):
            ser.write(pkt)
            r = wait_byte(ser, 8)
            if r == 0x06:
                acked = True
                break
            if r == 0x18:
                print('RECEIVER CANCEL at block', i)
                return 4
        if not acked:
            print('NO ACK at block', i)
            return 5
        seq = (seq + 1) & 0xFF
        if i % 128 == 0:
            print('block %d/%d' % (i, nblocks), flush=True)

    eot_ok = False
    for _ in range(10):
        ser.write(b'\x04')
        if wait_byte(ser, 8) == 0x06:
            eot_ok = True
            break

    time.sleep(1.0)
    out = b''
    end = time.time() + 2
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            out += chunk
            end = time.time() + 0.5
    ser.close()
    print('EOT ack=%s blocks=%d' % (eot_ok, nblocks))
    print('--- UBOOT OUTPUT ---')
    print(out.decode('ascii', 'replace'))
    return 0 if eot_ok else 6


if __name__ == '__main__':
    sys.exit(main())
