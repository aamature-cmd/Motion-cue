package com.example.motionstream

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.os.SystemClock
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.RadioGroup
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : Activity() {

    private lateinit var linkGroup: RadioGroup
    private lateinit var hostField: EditText
    private lateinit var portField: EditText
    private lateinit var rateField: EditText
    private lateinit var batchField: EditText
    private lateinit var saverSwitch: Switch
    private lateinit var toggle: Button
    private lateinit var status: TextView
    private lateinit var networks: TextView

    private var link = "hotspot"
    private var lastActionAt = 0L

    private val poll = object : Runnable {
        override fun run() {
            // Right after a tap the service may not have started yet, so trust the tap for a moment.
            if (SystemClock.elapsedRealtime() - lastActionAt > 2000) render(StreamService.running)
            toggle.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        linkGroup = findViewById(R.id.link)
        hostField = findViewById(R.id.host)
        portField = findViewById(R.id.port)
        rateField = findViewById(R.id.rate)
        batchField = findViewById(R.id.batch)
        saverSwitch = findViewById(R.id.saver)
        toggle = findViewById(R.id.toggle)
        status = findViewById(R.id.status)
        networks = findViewById(R.id.networks)

        val p = StreamConfig.prefs(this)
        link = p.getString("link", "hotspot") ?: "hotspot"
        linkGroup.check(if (link == "usb") R.id.linkUsb else R.id.linkHotspot)
        hostField.setText(p.getString("host_$link", ""))
        portField.setText(p.getInt("port", 8765).toString())
        rateField.setText(p.getInt("rate", 60).toString())
        batchField.setText(p.getInt("batch", 30).toString())
        saverSwitch.isChecked = p.getBoolean("saver", false)

        linkGroup.setOnCheckedChangeListener { _, id ->
            val prefs = StreamConfig.prefs(this)
            prefs.edit().putString("host_$link", hostField.text.toString().trim()).apply()
            link = if (id == R.id.linkUsb) "usb" else "hotspot"
            hostField.setText(prefs.getString("host_$link", ""))
        }
        toggle.setOnClickListener { if (StreamService.running) stopStreaming() else startStreaming() }
        findViewById<Button>(R.id.battery).setOnClickListener {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    override fun onResume() {
        super.onResume()
        render(StreamService.running)
        toggle.postDelayed(poll, 1000)
    }

    override fun onPause() {
        toggle.removeCallbacks(poll)
        super.onPause()
    }

    private fun startStreaming() {
        val host = hostField.text.toString().trim()
        val port = portField.text.toString().toIntOrNull()
        if (host.isEmpty() || port == null || port !in 1..65535) {
            Toast.makeText(this, "Enter the PC address and a port from 1 to 65535", Toast.LENGTH_LONG).show()
            return
        }
        val rate = (rateField.text.toString().toIntOrNull() ?: 60).coerceIn(50, 100)
        val batch = (batchField.text.toString().toIntOrNull() ?: 30).coerceIn(0, 200)
        rateField.setText(rate.toString())
        batchField.setText(batch.toString())

        StreamConfig.prefs(this).edit()
            .putString("link", link)
            .putString("host_$link", host)
            .putInt("port", port)
            .putInt("rate", rate)
            .putInt("batch", batch)
            .putBoolean("saver", saverSwitch.isChecked)
            .apply()

        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)  // the service runs either way
        }
        startForegroundService(Intent(this, StreamService::class.java))
        lastActionAt = SystemClock.elapsedRealtime()
        render(true)
    }

    private fun stopStreaming() {
        stopService(Intent(this, StreamService::class.java))
        lastActionAt = SystemClock.elapsedRealtime()
        render(false)
    }

    private fun render(streaming: Boolean) {
        toggle.text = if (streaming) "Stop streaming" else "Start streaming"
        val exempt = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(packageName)
        status.text = (if (streaming) "Streaming" else "Idle") +
            "\nBattery optimization: " + (if (exempt) "exempt" else "on (the system may pause streaming)")
        networks.text = "This phone's networks\n" + localNetworks()
    }

    /** Lists IPv4 addresses so you can spot the tether link (rndis0, usb0, ncm0, ap0, swlan0...). */
    private fun localNetworks(): String = try {
        val lines = NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { ni ->
                ni.interfaceAddresses
                    .filter { it.address is Inet4Address }
                    .map { "${ni.name}  ${it.address.hostAddress}/${it.networkPrefixLength}" }
            }
        if (lines.isEmpty()) "none" else lines.joinToString("\n")
    } catch (e: Exception) {
        "unavailable"
    }
}
