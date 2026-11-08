# Motion overlay

A phone streams motion over UDP; a click-through overlay on the PC drifts a ceramic-frit edge pattern with it.

```
Android app (foreground service) ──UDP, 50–100 Hz──► overlay.py (PyQt5)
accelerometer + gyroscope + rotation vector          hotspot or USB tethering
```

## PC

```
pip install PyQt5
python overlay.py --stats
```

Allow the port through Windows Firewall once (admin prompt):

```
netsh advfirewall firewall add rule name="Motion overlay" dir=in action=allow protocol=UDP localport=8765
```

Test without a phone: `python fake_phone.py` streams a synthetic drive to 127.0.0.1.

Useful flags: `--dots N` (default 300), `--band PX`, `--port`, `--stats`.

Axis mapping depends on how the phone sits in the car. Name the phone axis that points to the vehicle's right
(`--lateral`) and forward (`--forward`); a trailing `-` flips an axis.

| Phone position | Flags |
| --- | --- |
| Flat, screen up, top edge pointing forward | `--lateral x --forward y` (default) |
| Upright in a mount, screen facing you | `--lateral x --forward z-` |

Dots drift opposite to the vehicle's acceleration. If an axis feels backwards, flip `SIGN_LATERAL` or `SIGN_FORWARD` at the top of
`overlay.py`.

## Android app

Open `android/` in Android Studio and run it on the phone (Android 8.0+). The versions in the Gradle files are a baseline;
accept Studio's upgrade suggestions if it offers any. There are no library dependencies.

1. Pick **Wi-Fi hotspot** or **USB tethering**, and enter the PC's address (each link remembers its own).
2. Port 8765, rate 50–100 Hz, batch window in ms (higher = fewer wakeups, more latency).
3. Tap **Start streaming**. The notification keeps it alive with the screen locked; use its Stop button or the app to stop.
4. Tap **Battery optimization settings** and exempt the app. Some manufacturers (Xiaomi, Huawei, Samsung) also have their own
   app-killer setting.

### Finding the PC's address

- **Hotspot:** turn on the phone's hotspot, join it from the PC, read the PC's address from `ipconfig` (or the line `overlay.py` prints).
- **USB tethering:** plug in the cable, enable *USB tethering* in the phone's hotspot and tethering settings, and read the PC address the
  same way. The app lists the phone's own addresses (for example `rndis0  192.168.42.129/24`); the PC is on the same subnet.
  USB has lower latency than Wi-Fi, no radio power-save delay, and it charges the phone.

## Wire format (little-endian)

| Part | Layout |
| --- | --- |
| Header, 10 bytes | `"MO"`, version `1`, flags (bit 0 gyro valid, bit 1 rotation valid), frame count, reserved, `u32` sequence |
| Frame, 48 bytes | `u64` sensor timestamp (ns), accel x y z, gyro x y z, rotation quaternion x y z w (all `float32`) |

A datagram holds 1–16 frames. The overlay uses the newest frame of the newest datagram, and drops duplicates and
out-of-order datagrams using the sequence number. Gravity is removed on the PC by rotating it out with the quaternion.

## Battery

- Sensors register with a report latency (batching), so the app wakes once per batch, not per sample.
- Every frame in a batch shares one datagram, so the radio sends one packet per batch.
- The game rotation vector is used when present. It needs no magnetometer.
- Optional **Let the CPU sleep between batches** uses wake-up sensors and no wake lock. It falls back to a wake lock when
  any sensor has no wake-up variant. If streaming stalls while locked, turn it off.
- `adb logcat -s MotionStream` shows each sensor's FIFO size and whether it supports batching.

## Getting an APK without installing Android Studio

Put this whole folder (including `.github/`) in a new GitHub repository. The *Build APK* workflow runs on every push;
open the run under the **Actions** tab and download `motionstream-apk` (a zip containing `app-debug.apk`). The debug build is
signed automatically, so it installs directly.

Installing on a Samsung Galaxy S24:

1. Copy `app-debug.apk` to the phone and open it. Allow "Install unknown apps" for the app you opened it from.
2. If the install is blocked, turn off **Settings > Security and privacy > Auto Blocker** (or its "Block software updates / unauthorized apps" option), then retry. Turn it back on afterwards.
3. Allow notifications when asked.
4. For reliable streaming with the screen locked: **Settings > Battery > Background usage limits > Never auto sleeping apps**, add Motion Stream, and set its battery usage to Unrestricted.
5. USB tethering: **Settings > Connections > Mobile Hotspot and Tethering > USB tethering**, with the cable plugged in.
