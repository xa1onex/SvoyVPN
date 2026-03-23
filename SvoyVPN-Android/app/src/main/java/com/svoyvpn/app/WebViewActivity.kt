package com.svoyvpn.app

import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ImageButton
import androidx.activity.OnBackPressedCallback
import androidx.fragment.app.FragmentActivity
import com.svoyvpn.app.auth.AuthActivity
import com.svoyvpn.app.auth.TokenStorage

class WebViewActivity : FragmentActivity() {

    private lateinit var webView: WebView
    private lateinit var tokenStorage: TokenStorage

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        tokenStorage = TokenStorage(this)

        // Root layout: WebView only
        val root = FrameLayout(this)
        setContentView(root)

        webView = WebView(this)
        root.addView(webView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ))

        setupWebView()

        val token = tokenStorage.getToken() ?: ""
        val isDark = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
                Configuration.UI_MODE_NIGHT_YES
        val themeMode = if (isDark) "dark" else "light"
        
        val miniAppUrl = "${BuildConfig.API_BASE_URL.trimEnd('/')}/miniapp?jwt=${token}&theme=${themeMode}"
        webView.loadUrl(miniAppUrl)

        // Handle back press via webView history
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack()
            }
        })
    }

    private fun setupWebView() {
        val isDark = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
                Configuration.UI_MODE_NIGHT_YES
        val colorScheme = if (isDark) "dark" else "light"
        val bgColor = if (isDark) "#18222d" else "#ffffff"

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            cacheMode = WebSettings.LOAD_DEFAULT
            setSupportZoom(false)
            builtInZoomControls = false
            displayZoomControls = false
            mediaPlaybackRequiresUserGesture = false
        }

        // Set background matching mini-app theme to avoid white flash
        webView.setBackgroundColor(if (isDark) Color.parseColor("#18222d") else Color.WHITE)

        webView.webChromeClient = WebChromeClient()

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                if (url == null) return false
                if (!url.startsWith("https://xdoublegroup.online") &&
                    !url.startsWith("http://localhost")
                ) {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    return true
                }
                return false
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                // Inject JWT and theme AFTER page has loaded
                injectAndroidBridge(colorScheme, bgColor)
            }
        }

        webView.addJavascriptInterface(NativeBridge(), "AndroidBridge")
    }

    private fun injectAndroidBridge(colorScheme: String, bgColor: String) {
        val token = tokenStorage.getToken() ?: return
        // Escape the token just in case (JWT tokens are alphanumeric + . - _)
        val safeToken = token.replace("\"", "")

        val js = """
            (function() {
                // Inject JWT for Android mode
                window.__androidJwt = "$safeToken";
                
                // Mock Telegram.WebApp so the mini-app doesn't break
                if (!window.Telegram) window.Telegram = {};
                if (!window.Telegram.WebApp) {
                    window.Telegram.WebApp = {
                        colorScheme: "$colorScheme",
                        themeParams: {
                            bg_color: "$bgColor",
                            secondary_bg_color: "${if (colorScheme == "dark") "#21303f" else "#f7f9fb"}",
                            text_color: "${if (colorScheme == "dark") "#ffffff" else "#000000"}",
                            hint_color: "${if (colorScheme == "dark") "#8e9db0" else "#999999"}",
                            accent_text_color: "#3aa8fc"
                        },
                        initData: "",
                        initDataUnsafe: { user: null },
                        ready: function() {},
                        expand: function() {},
                        onEvent: function() {},
                        setHeaderColor: function() {},
                        setBackgroundColor: function() {},
                        setBottomBarColor: function() {},
                        openLink: function(url) { window.open(url, '_blank'); },
                        openInvoice: function(url) { window.open(url, '_blank'); },
                        HapticFeedback: { impactOccurred: function() {} }
                    };
                }
                
                // Re-apply theme immediately
                var scheme = "$colorScheme";
                document.documentElement.setAttribute('data-theme', scheme);
                document.body.setAttribute('data-theme', scheme);
                var meta = document.querySelector('meta[name="theme-color"]');
                if (meta) meta.setAttribute('content', "$bgColor");
                
                // Trigger loadUser if app is already initialized
                if (typeof loadUser === 'function') loadUser();
                
                window.__androidLogout = function() {
                    if (window.AndroidBridge) AndroidBridge.logout();
                };

                window.haptic = function(style) {
                    if (window.AndroidBridge) AndroidBridge.vibrate(style);
                };
            })();
        """.trimIndent()

        webView.evaluateJavascript(js, null)
    }

    private fun showLogoutDialog() {
        android.app.AlertDialog.Builder(this)
            .setTitle("Выход из аккаунта")
            .setMessage("Вы уверены, что хотите выйти?")
            .setPositiveButton("Выйти") { _, _ -> doLogout() }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun doLogout() {
        tokenStorage.clearToken()
        startActivity(Intent(this, AuthActivity::class.java))
        finish()
    }

    inner class NativeBridge {
        @JavascriptInterface
        fun logout() {
            runOnUiThread { showLogoutDialog() }
        }

        @JavascriptInterface
        fun share(text: String) {
            runOnUiThread {
                val intent = Intent(Intent.ACTION_SEND).apply {
                    type = "text/plain"
                    putExtra(Intent.EXTRA_TEXT, text)
                }
                startActivity(Intent.createChooser(intent, "Поделиться"))
            }
        }

        @JavascriptInterface
        fun vibrate(style: String) {
            val vibrator = getSystemService(android.content.Context.VIBRATOR_SERVICE) as? android.os.Vibrator
            if (vibrator != null && vibrator.hasVibrator()) {
                when (style) {
                    "light" -> {
                        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                            vibrator.vibrate(android.os.VibrationEffect.createOneShot(20, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
                        } else {
                            vibrator.vibrate(20)
                        }
                    }
                    "medium" -> {
                        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                            vibrator.vibrate(android.os.VibrationEffect.createOneShot(40, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
                        } else {
                            vibrator.vibrate(40)
                        }
                    }
                    "success" -> {
                        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                            vibrator.vibrate(android.os.VibrationEffect.createWaveform(longArrayOf(0, 30, 100, 30), -1))
                        } else {
                            vibrator.vibrate(longArrayOf(0, 30, 100, 30), -1)
                        }
                    }
                    "error" -> {
                        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                            vibrator.vibrate(android.os.VibrationEffect.createWaveform(longArrayOf(0, 50, 100, 50, 100, 50), -1))
                        } else {
                            vibrator.vibrate(longArrayOf(0, 50, 100, 50, 100, 50), -1)
                        }
                    }
                }
            }
        }

        @JavascriptInterface
        fun getToken(): String = tokenStorage.getToken() ?: ""
    }
}
