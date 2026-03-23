package com.svoyvpn.app.notification

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import com.svoyvpn.app.R
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

class SvoyVpnMessagingService : FirebaseMessagingService() {

    override fun onMessageReceived(remoteMessage: RemoteMessage) {
        val title = remoteMessage.notification?.title ?: "SvoyVPN"
        val body = remoteMessage.notification?.body ?: return

        val notification = NotificationCompat.Builder(this, NotificationHelper.CHANNEL_SUBSCRIPTION)
            .setSmallIcon(R.drawable.ic_vpn_key)
            .setContentTitle(title)
            .setContentText(body)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setAutoCancel(true)
            .build()

        if (NotificationManagerCompat.from(this).areNotificationsEnabled()) {
            NotificationManagerCompat.from(this).notify(1002, notification)
        }
    }

    override fun onNewToken(token: String) {
        // TODO: send FCM token to backend for server-initiated pushes
    }
}
