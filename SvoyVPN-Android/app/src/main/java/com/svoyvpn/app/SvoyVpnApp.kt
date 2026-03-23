package com.svoyvpn.app

import android.app.Application
import com.svoyvpn.app.auth.TokenStorage
import com.svoyvpn.app.notification.NotificationHelper
import com.svoyvpn.app.notification.SubscriptionWorker

class SvoyVpnApp : Application() {

    lateinit var tokenStorage: TokenStorage
        private set

    override fun onCreate() {
        super.onCreate()

        tokenStorage = TokenStorage(this)

        // Setup notification channels
        NotificationHelper.createChannels(this)

        // Schedule daily subscription expiry check
        SubscriptionWorker.schedule(this)
    }
}
