package com.svoyvpn.app.notification

import android.content.Context
import androidx.work.*
import com.svoyvpn.app.BuildConfig
import com.svoyvpn.app.auth.TokenStorage
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.time.LocalDate
import java.time.temporal.ChronoUnit
import java.util.concurrent.TimeUnit

/**
 * WorkManager worker that checks subscription expiry daily
 * and posts notifications at 1-3 days remaining.
 * Uses plain HttpURLConnection — no Retrofit needed.
 */
class SubscriptionWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        return try {
            val tokenStorage = TokenStorage(applicationContext)
            val token = tokenStorage.getToken() ?: return Result.success()

            val baseUrl = BuildConfig.API_BASE_URL.trimEnd('/')
            val url = URL("$baseUrl/api/user")
            val conn = (url.openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Accept", "application/json")
                connectTimeout = 10_000
                readTimeout = 10_000
            }

            if (conn.responseCode == HttpURLConnection.HTTP_OK) {
                val body = conn.inputStream.bufferedReader().readText()
                val json = JSONObject(body)
                val sub = json.optJSONObject("subscription") ?: return Result.success()
                val isActive = sub.optBoolean("isActive", false)
                val endDateStr = sub.optString("endDate", null)

                if (isActive && !endDateStr.isNullOrEmpty()) {
                    val end = LocalDate.parse(endDateStr.take(10))
                    val daysLeft = ChronoUnit.DAYS.between(LocalDate.now(), end).toInt()
                    if (daysLeft in 0..3) {
                        NotificationHelper.showSubscriptionExpiryNotification(applicationContext, daysLeft)
                    }
                }
            }
            conn.disconnect()
            Result.success()
        } catch (_: Exception) {
            Result.retry()
        }
    }

    companion object {
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<SubscriptionWorker>(1, TimeUnit.DAYS)
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                "subscription_check",
                ExistingPeriodicWorkPolicy.KEEP,
                request
            )
        }
    }
}
