#!/bin/sh
# Имя dnevnik.lan всегда ведёт на «Мой дневник», каким бы ни был адрес роутера в домашней сети (192.168.1.1, 192.168.73.1 …).
#   setname.sh          - включить/обновить имя (запускается при старте службы и когда у роутера меняется адрес)
#   setname.sh remove   - убрать имя и вернуть стартовую страницу роутера как была
#
# Что делает: 1) добавляет в DNS-сервер роутера запись «dnevnik» -> текущий адрес роутера (раздел dhcp.dnevnik_name);
# 2) на стартовой странице роутера (LuCI, порт 80) делает так, что адрес http://dnevnik.lan без номера порта открывает «Мой дневник»,
# а все остальные адреса (в том числе по цифрам) открывают роутер, как раньше.
PORT=8765
INDEX=/www/index.html
BACKUP=/www/index.html.dnevnik-orig
HOTPLUG=/etc/hotplug.d/iface/95-dnevnik
APP=/root/dnevnik

lan_ip() {
    ip=$(ubus call network.interface.lan status 2>/dev/null | jsonfilter -e '@["ipv4-address"][0].address' 2>/dev/null)
    [ -n "$ip" ] || ip=$(uci -q get network.lan.ipaddr | cut -d/ -f1)
    echo "$ip"
}

dns_restart() { /etc/init.d/dnsmasq restart >/dev/null 2>&1; }

redirect_on() {
    [ -f "$INDEX" ] || return 0
    grep -q 'dnevnik-openwrt' "$INDEX" && return 0                 # уже включено
    grep -q 'http-equiv="refresh" content="0; URL=cgi-bin/luci/"' "$INDEX" || return 0   # страница не такая, как ожидаем: не трогаем
    cp "$INDEX" "$BACKUP"
    sed 's#<meta http-equiv="refresh" content="0; URL=cgi-bin/luci/" />#<script>/* dnevnik-openwrt */ if (/^dnevnik(\\.|$)/i.test(location.hostname)) { location.replace("http://" + location.hostname + ":'"$PORT"'/"); } else { location.replace("cgi-bin/luci/"); }</script><noscript><meta http-equiv="refresh" content="0; URL=cgi-bin/luci/" /></noscript>#' \
        "$BACKUP" > "$INDEX.new" && grep -q 'dnevnik-openwrt' "$INDEX.new" && chmod 644 "$INDEX.new" && mv "$INDEX.new" "$INDEX" || rm -f "$INDEX.new"
}

redirect_off() {
    [ -f "$BACKUP" ] && mv "$BACKUP" "$INDEX"
    return 0
}

apply() {
    IP=$(lan_ip)
    [ -n "$IP" ] || { echo "не удалось узнать адрес роутера"; return 1; }
    if [ "$(uci -q get dhcp.dnevnik_name.ip)" != "$IP" ] || [ "$(uci -q get dhcp.dnevnik_name.name)" != "dnevnik" ]; then
        uci set dhcp.dnevnik_name=domain
        uci set dhcp.dnevnik_name.name=dnevnik
        uci set dhcp.dnevnik_name.ip="$IP"
        uci commit dhcp
        dns_restart
    fi
    # при смене адреса роутера имя обновится само
    cat > "$HOTPLUG" <<HP
#!/bin/sh
[ "\$ACTION" = "ifup" ] && [ "\$INTERFACE" = "lan" ] && $APP/router/setname.sh >/dev/null 2>&1
exit 0
HP
    chmod 755 "$HOTPLUG"
    redirect_on
}

remove() {
    uci -q delete dhcp.dnevnik_name && uci commit dhcp && dns_restart
    rm -f "$HOTPLUG"
    redirect_off
}

case "$1" in
    remove) remove ;;
    *) apply ;;
esac
