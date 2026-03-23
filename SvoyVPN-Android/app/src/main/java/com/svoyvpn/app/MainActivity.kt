package com.svoyvpn.app

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.fragment.app.FragmentActivity
import com.svoyvpn.app.auth.AuthActivity
import com.svoyvpn.app.auth.TokenStorage

class MainActivity : FragmentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val tokenStorage = TokenStorage(this)

        // Handle Telegram auth deep link callback: svoyvpn://auth?token=JWT
        val uri: Uri? = intent?.data
        if (uri != null && uri.scheme == "svoyvpn" && uri.host == "auth") {
            val token = uri.getQueryParameter("token")
            if (!token.isNullOrEmpty()) {
                tokenStorage.saveToken(token)
                startActivity(Intent(this, WebViewActivity::class.java))
                finish()
                return
            }
        }

        if (tokenStorage.isLoggedIn()) {
            startActivity(Intent(this, WebViewActivity::class.java))
        } else {
            startActivity(Intent(this, AuthActivity::class.java))
        }
        finish()
    }
}
