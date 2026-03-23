package com.svoyvpn.app.widget

import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.widget.RemoteViews
import com.svoyvpn.app.R
import com.svoyvpn.app.auth.TokenStorage

class VpnStatusWidget : AppWidgetProvider() {

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        val tokenStorage = TokenStorage(context)
        val isLoggedIn = tokenStorage.isLoggedIn()

        appWidgetIds.forEach { widgetId ->
            val views = RemoteViews(context.packageName, R.layout.vpn_widget_layout).apply {
                setTextViewText(R.id.widget_title, "SvoyVPN")
                setTextViewText(
                    R.id.widget_status,
                    if (isLoggedIn) "Открыть приложение" else "Войдите в аккаунт"
                )
            }
            appWidgetManager.updateAppWidget(widgetId, views)
        }
    }
}
