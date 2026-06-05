#!/bin/bash
# Boot-time AP state sync. Run once by rover2-ap-boot.service after NM settles.
# Install: sudo cp rover2-ap-sync.sh /usr/local/bin/rover2-ap-sync.sh
#          sudo chmod 755 /usr/local/bin/rover2-ap-sync.sh
#
# If wlan0 is connected to home WiFi: AP off (save power).
# If wlan0 not connected: AP on (ROVER2 SSID available away from home).

if nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -q "^wlan0:connected"; then
    logger -t rover2-ap "boot: wlan0 connected -- AP disabled"
    systemctl stop hostapd
    systemctl stop dnsmasq
else
    logger -t rover2-ap "boot: wlan0 not connected -- AP enabled"
    systemctl start hostapd
    sleep 1
    systemctl restart dnsmasq
fi
