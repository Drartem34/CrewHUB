package ua.crewhub.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Після перезавантаження телефона чи оновлення застосунку знову вмикає стеження за робочим Wi-Fi. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent i) {
        WatchService.start(c);
    }
}
