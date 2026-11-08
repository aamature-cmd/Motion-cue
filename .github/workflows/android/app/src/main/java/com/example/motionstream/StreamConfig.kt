package com.example.motionstream

import android.content.Context
import android.content.SharedPreferences

/** What the service streams and where. The activity writes it, the service reads it at start. */
data class StreamConfig(
    val host: String,
    val port: Int,
    val rateHz: Int,
    val batchMs: Int,
    val saver: Boolean,
) {
    companion object {
        fun prefs(ctx: Context): SharedPreferences =
            ctx.getSharedPreferences("motionstream", Context.MODE_PRIVATE)

        fun load(ctx: Context): StreamConfig {
            val p = prefs(ctx)
            // Hotspot and USB tethering keep separate PC addresses: the PC gets a different IP on each link.
            val link = p.getString("link", "hotspot") ?: "hotspot"
            return StreamConfig(
                host = p.getString("host_$link", "") ?: "",
                port = p.getInt("port", 8765),
                rateHz = p.getInt("rate", 60).coerceIn(50, 100),
                batchMs = p.getInt("batch", 30).coerceIn(0, 200),
                saver = p.getBoolean("saver", false),
            )
        }
    }
}
