package com.svoyvpn.app.notification

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.svoyvpn.app.R

object NotificationHelper {

    const val CHANNEL_SUBSCRIPTION = "subscription_expiry"

    fun createChannels(context: Context) {
        val channel = NotificationChannel(
            CHANNEL_SUBSCRIPTION,
            "Подписка",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Уведомления об истечении подписки"
        }
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(channel)
    }

    fun showSubscriptionExpiryNotification(context: Context, daysLeft: Int) {
        val title = "SvoyVPN — подписка истекает"
        val message = when (daysLeft) {
            0 -> "Ваша подписка истекает сегодня!"
            1 -> "Ваша подписка истекает завтра"
            else -> "Ваша подписка истекает через $daysLeft дней"
        }

        val notification = NotificationCompat.Builder(context, CHANNEL_SUBSCRIPTION)
            .setSmallIcon(R.drawable.ic_vpn_key)
            .setContentTitle(title)
            .setContentText(message)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .build()

        if (NotificationManagerCompat.from(context).areNotificationsEnabled()) {
            NotificationManagerCompat.from(context).notify(1001, notification)
        }
    }
}
