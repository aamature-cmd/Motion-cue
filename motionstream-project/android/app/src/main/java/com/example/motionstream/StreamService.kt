package com.example.motionstream

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Log
import android.widget.Toast
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Streams accelerometer, gyroscope and rotation-vector data to the PC over UDP.
 *
 * Wire format (little-endian), decoded by overlay.py:
 *   header, 10 bytes: 'M' 'O', version = 1, flags (1 gyro valid, 2 rotation valid), frame count,
 *                     reserved, u32 sequence
 *   frame, 48 bytes:  u64 sensor timestamp (ns), accel xyz, gyro xyz, rotation quaternion x y z w
 *
 * One frame is produced per accelerometer event. Frames delivered together (a sensor batch) leave in a
 * single datagram, so the radio wakes once per batch instead of once per sample.
 */
class StreamService : Service(), SensorEventListener {

    companion object {
        /** True from onCreate to onDestroy; the activity uses it to label its button. */
        @Volatile
        var running = false

        const val ACTION_STOP = "com.example.motionstream.STOP"

        private const val TAG = "MotionStream"
        private const val CHANNEL = "stream"
        private const val NOTIFICATION_ID = 1
        private const val MAX_FRAMES = 16
        private const val HEADER_BYTES = 10
        private const val FRAME_BYTES = 48
        private const val FLAG_GYRO = 1
        private const val FLAG_ROT = 2
        private const val WAKELOCK_TIMEOUT_MS = 12 * 60 * 60 * 1000L  // safety net if the service is never stopped
    }

    private lateinit var thread: HandlerThread
    private lateinit var handler: Handler
    @Volatile private var sensorManager: SensorManager? = null
    private var socket: DatagramSocket? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var started = false

    // Latest values from the secondary sensors; the accelerometer drives frame emission.
    private val gyro = FloatArray(3)
    private val quat = FloatArray(4)   // x, y, z, w
    private val wxyz = FloatArray(4)   // scratch for SensorManager.getQuaternionFromVector (w, x, y, z)
    private var haveGyro = false
    private var haveRot = false

    private val out = ByteArray(HEADER_BYTES + MAX_FRAMES * FRAME_BYTES)
    private val bb = ByteBuffer.wrap(out).order(ByteOrder.LITTLE_ENDIAN)
    private val packet = DatagramPacket(out, out.size)
    private var frames = 0
    private var seq = 0
    private var flushPosted = false
    private var minGapNs = 0L
    private var lastFrameNs = 0L
    private var sendErrors = 0
    private val flushTask = Runnable { flush() }

    override fun onCreate() {
        super.onCreate()
        thread = HandlerThread("motion-stream").also { it.start() }
        handler = Handler(thread.looper)
        running = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        val config = StreamConfig.load(this)
        enterForeground(config)  // must happen within seconds of startForegroundService()
        if (!started) {
            started = true
            handler.post { begin(config) }
        }
        return START_NOT_STICKY
    }

    private fun enterForeground(config: StreamConfig) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "Streaming", NotificationManager.IMPORTANCE_LOW)
        )
        val stop = PendingIntent.getService(
            this, 0,
            Intent(this, StreamService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("Streaming motion data")
            .setContentText("${config.host}:${config.port} at ${config.rateHz} Hz")
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop", stop)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    /** Runs on the sensor thread: opens the socket, picks sensors, registers with batching. */
    private fun begin(config: StreamConfig) {
        try {
            val address = InetAddress.getByName(config.host)
            // A connected socket is cheaper per send and reports "port unreachable" on later sends.
            socket = DatagramSocket().also { it.connect(InetSocketAddress(address, config.port)) }
        } catch (e: Exception) {
            fail("Can't use ${config.host}:${config.port} (${e.javaClass.simpleName})")
            return
        }

        val sm = getSystemService(SensorManager::class.java)
        sensorManager = sm
        val accel = find(sm, Sensor.TYPE_ACCELEROMETER, config.saver)
        val gyroSensor = find(sm, Sensor.TYPE_GYROSCOPE, config.saver)
        // The game rotation vector fuses accelerometer and gyroscope only. It skips the magnetometer
        // (cheaper, and unaffected by the car body). It falls back to the compass-based vector.
        val rotation = find(sm, Sensor.TYPE_GAME_ROTATION_VECTOR, config.saver)
            ?: find(sm, Sensor.TYPE_ROTATION_VECTOR, config.saver)
        if (accel == null) {
            fail("This phone has no accelerometer")
            return
        }
        val sensors = listOfNotNull(accel, gyroSensor, rotation)

        // Wake-up sensors let the CPU sleep between batches. If any sensor lacks a wake-up variant (or the
        // option is off) keep the CPU awake instead, otherwise streaming stops once the screen locks.
        val canSleep = config.saver && sensors.all { it.isWakeUpSensor }
        if (!canSleep) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "motionstream:stream")
                .apply {
                    setReferenceCounted(false)
                    acquire(WAKELOCK_TIMEOUT_MS)
                }
        }

        val periodUs = 1_000_000 / config.rateHz
        // Sensors may deliver faster than requested; drop frames closer together than this.
        minGapNs = (1_000_000_000L / config.rateHz * 0.7).toLong()
        for (sensor in sensors) {
            Log.i(TAG, "${sensor.name}: fifo=${sensor.fifoMaxEventCount} wakeUp=${sensor.isWakeUpSensor} " +
                "minDelay=${sensor.minDelay}us")
            sm.registerListener(this, sensor, periodUs, config.batchMs * 1000, handler)
        }
        Log.i(TAG, "streaming to ${config.host}:${config.port} at ${config.rateHz} Hz, " +
            "batch ${config.batchMs} ms, cpuSleep=$canSleep")
    }

    private fun find(sm: SensorManager, type: Int, wakeUp: Boolean): Sensor? =
        (if (wakeUp) sm.getDefaultSensor(type, true) else null) ?: sm.getDefaultSensor(type)

    override fun onSensorChanged(e: SensorEvent) {
        when (e.sensor.type) {
            Sensor.TYPE_ACCELEROMETER -> {
                if (e.timestamp - lastFrameNs < minGapNs) return
                lastFrameNs = e.timestamp
                addFrame(e.timestamp, e.values)
            }
            Sensor.TYPE_GYROSCOPE -> {
                gyro[0] = e.values[0]; gyro[1] = e.values[1]; gyro[2] = e.values[2]
                haveGyro = true
            }
            Sensor.TYPE_GAME_ROTATION_VECTOR, Sensor.TYPE_ROTATION_VECTOR -> {
                SensorManager.getQuaternionFromVector(wxyz, e.values)
                quat[0] = wxyz[1]; quat[1] = wxyz[2]; quat[2] = wxyz[3]; quat[3] = wxyz[0]
                haveRot = true
            }
        }
    }

    override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit

    private fun addFrame(timestampNs: Long, accel: FloatArray) {
        val o = HEADER_BYTES + frames * FRAME_BYTES
        bb.putLong(o, timestampNs)
        bb.putFloat(o + 8, accel[0]); bb.putFloat(o + 12, accel[1]); bb.putFloat(o + 16, accel[2])
        bb.putFloat(o + 20, gyro[0]); bb.putFloat(o + 24, gyro[1]); bb.putFloat(o + 28, gyro[2])
        bb.putFloat(o + 32, quat[0]); bb.putFloat(o + 36, quat[1]); bb.putFloat(o + 40, quat[2])
        bb.putFloat(o + 44, quat[3])
        frames++
        if (frames == MAX_FRAMES) {
            flush()
        } else if (!flushPosted) {
            // Sensor batches are dispatched inside one looper callback. A task posted now runs right
            // after that callback, so every frame of the batch is already in the buffer. No timers needed.
            flushPosted = true
            handler.post(flushTask)
        }
    }

    private fun flush() {
        flushPosted = false
        if (frames == 0) return
        out[0] = 'M'.code.toByte()
        out[1] = 'O'.code.toByte()
        out[2] = 1
        out[3] = ((if (haveGyro) FLAG_GYRO else 0) or (if (haveRot) FLAG_ROT else 0)).toByte()
        out[4] = frames.toByte()
        out[5] = 0
        bb.putInt(6, seq++)
        packet.length = HEADER_BYTES + frames * FRAME_BYTES
        frames = 0
        try {
            socket?.send(packet)
            sendErrors = 0
        } catch (e: IOException) {
            // PC not listening yet, or the link is down: keep going and recover by itself.
            if (sendErrors++ == 0) Log.w(TAG, "send failed: ${e.message}")
        }
    }

    private fun fail(message: String) {
        Log.w(TAG, message)
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(applicationContext, message, Toast.LENGTH_LONG).show()
        }
        stopSelf()
    }

    override fun onDestroy() {
        running = false
        sensorManager?.unregisterListener(this)
        handler.post {
            socket?.close()
            socket = null
            wakeLock?.let { if (it.isHeld) it.release() }
            wakeLock = null
            thread.quitSafely()
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
