# -*- coding: utf-8 -*-
"""Делает демо-копию интерфейса: настоящий index.html + подставной бэкенд.
Личных данных внутри нет — имена серверов и профиль вымышленные."""
import os

ROOT = r"C:\Users\DanDev\Desktop\Новая папка (2)"
SRC = os.path.join(ROOT, "ui", "index.html")
OUT_DIR = os.path.join(ROOT, "demoloop")
OUT = os.path.join(OUT_DIR, "demo.html")

MOCK = r"""
<script>
/* ---- подставной бэкенд: приложение думает, что говорит с Python ---- */
(function(){
  /* аватарка-заглушка: первая буква ника на тёмном круге */
  function makeAvatar(u){
    var ch = (u.charAt(0) || "?").toUpperCase();
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
      + '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
      + '<stop offset="0" stop-color="#3a3a3f"/><stop offset="1" stop-color="#141416"/>'
      + '</linearGradient></defs>'
      + '<rect width="100" height="100" rx="50" fill="url(#g)"/>'
      + '<text x="50" y="50" fill="#fff" font-size="46" font-weight="700"'
      + ' font-family="Segoe UI,Arial,sans-serif" text-anchor="middle"'
      + ' dominant-baseline="central">' + ch + '</text></svg>';
    return "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));
  }

  /* \u0421\u0435\u0440\u0432\u0435\u0440\u043e\u0432 \u0441\u043d\u0430\u0447\u0430\u043b\u0430 \u043d\u0435\u0442 \u2014 \u0438\u043d\u0430\u0447\u0435 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0441\u043f\u0440\u0430\u0432\u0435\u0434\u043b\u0438\u0432\u043e \u0440\u0435\u0448\u0438\u0442, \u0447\u0442\u043e \u0437\u043d\u0430\u043a\u043e\u043c\u0441\u0442\u0432\u043e
     \u0443\u0436\u0435 \u043f\u0440\u043e\u0439\u0434\u0435\u043d\u043e, \u0438 \u043f\u043e\u043a\u0430\u0436\u0435\u0442 \u0433\u043b\u0430\u0432\u043d\u044b\u0439 \u044d\u043a\u0440\u0430\u043d. \u041f\u043e\u044f\u0432\u044f\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0438\u043d\u0442\u0440\u043e. */
  var SERVERS = [
      {name:"\ud83c\uddf5\ud83c\uddf1 POLAND",   protocol:"vless", address:"pl-01.example.net", port:443, network:"tcp"},
      {name:"\ud83c\udde9\ud83c\uddea GERMANY",  protocol:"vless", address:"de-03.example.net", port:443, network:"tcp"},
      {name:"\ud83c\uddf8\ud83c\uddea SWEDEN",   protocol:"vless", address:"se-02.example.net", port:443, network:"xhttp"},
      {name:"\ud83c\uddfa\ud83c\uddf8 USA",      protocol:"trojan", address:"us-01.example.net", port:8443, network:"tcp"}
  ];
  var S = {
    servers: [],
    selected: 0, connected: false, local_id: "LDK-0000", rating: 0, emoji: "\ud83e\uddca",
    xray_found: true, tun_found: true, is_admin: false, tun_active: false,
    intro_done: false, conflicts: [], save_error: "", data_dir: "%APPDATA%\\LDK2ray",
    speed: {up:0, down:0, total_up:0, total_down:0},
    sub: {known:true, title:"DEMO", announce:"\ud83d\udd04 \u0414\u0435\u043c\u043e\u043d\u0441\u0442\u0440\u0430\u0446\u0438\u043e\u043d\u043d\u044b\u0439 \u0440\u0435\u0436\u0438\u043c \u2014 \u0434\u0430\u043d\u043d\u044b\u0435 \u0432\u044b\u043c\u044b\u0448\u043b\u0435\u043d\u043d\u044b\u0435",
          used: 25000000000, total: 107374182400, left: 82374182400, percent: 23.3,
          expire: 1798761600, days_left: 158, expired: false, updated: 1785096000, url: "demo"},
    settings: {socks_port:10808, http_port:10809, system_proxy:true, tun_mode:false,
               theme:"dark", lang:"ru", minimize_to_tray:true, start_minimized:false,
               high_priority:false, tun_dns:"1.1.1.1", xray_path:"",
               subscription_url:"https://example.com/sub", route_mode:"global",
               direct_sites:[], block_sites:[], tg_username:"", tg_name:"", tg_avatar:""}
  };
  var wrap = function(){ return {ok:true, state:S}; };
  var speedTimer = null;

  window.pywebview = { api: {
    get_state:            function(){ return Promise.resolve(S); },
    tick_emoji:           function(){ return Promise.resolve({emoji:S.emoji}); },
    select_server:        function(i){ S.selected = i; return Promise.resolve(S); },
    delete_server:        function(i){ S.servers.splice(i,1); return Promise.resolve(S); },
    set_rating:           function(n){ S.rating = n; return Promise.resolve(S); },
    save_settings:        function(p){ Object.assign(S.settings, p||{}); var r=Object.assign({},S); r.saved=true; return Promise.resolve(r); },
    save_routing:         function(p){ p=p||{};
                            S.settings.route_mode = p.mode || "global";
                            var toList = function(v){ return (typeof v === "string" ? v.split("\n") : (v||[])).map(function(x){return String(x).trim();}).filter(Boolean); };
                            S.settings.direct_sites = toList(p.direct);
                            S.settings.block_sites  = toList(p.block);
                            return Promise.resolve(wrap()); },
    set_modes:            function(p){ p=p||{};
                            if(p.tun && !S.is_admin) return Promise.resolve({error:"need_admin", state:S});
                            S.settings.system_proxy = !!p.proxy; S.settings.tun_mode = !!p.tun;
                            return Promise.resolve(wrap()); },
    request_admin:        function(){ S.is_admin = true; return Promise.resolve({ok:true}); },
    /* \u041d\u0430\u0441\u0442\u043e\u044f\u0449\u0438\u0439 Telegram \u043e\u0442\u0441\u044e\u0434\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0438\u0436\u0438\u043c: \u0431\u0440\u0430\u0443\u0437\u0435\u0440 \u043d\u0435 \u043f\u0443\u0441\u043a\u0430\u0435\u0442 \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u0443\u044e
       \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0443 \u043d\u0430 t.me. \u041f\u043e\u044d\u0442\u043e\u043c\u0443 \u0438\u043c\u044f \u0441\u0442\u0440\u043e\u0438\u043c \u0438\u0437 \u0432\u0432\u0435\u0434\u0451\u043d\u043d\u043e\u0433\u043e username, \u0430 \u0430\u0432\u0430\u0442\u0430\u0440\u043a\u0443
       \u0440\u0438\u0441\u0443\u0435\u043c \u0441 \u0435\u0433\u043e \u043f\u0435\u0440\u0432\u043e\u0439 \u0431\u0443\u043a\u0432\u043e\u0439 \u2014 \u0447\u0442\u043e\u0431\u044b \u0434\u0435\u043c\u043e \u043e\u0442\u0437\u044b\u0432\u0430\u043b\u043e\u0441\u044c \u043d\u0430 \u0432\u0432\u043e\u0434. */
    link_telegram:        function(u){
                            u = String(u||"").replace(/^@/,"").split("/").pop().trim();
                            if(u.length < 4) return Promise.resolve({error:"bad_username"});
                            S.settings.tg_username = u;
                            S.settings.tg_name = u.charAt(0).toUpperCase() + u.slice(1);
                            S.settings.tg_avatar = makeAvatar(u);
                            return new Promise(function(res){ setTimeout(function(){ res(wrap()); }, 900); }); },
    unlink_telegram:      function(){ S.settings.tg_username=""; S.settings.tg_name=""; S.settings.tg_avatar=""; return Promise.resolve(wrap()); },
    open_external:        function(){ return Promise.resolve({ok:true}); },
    open_data_folder:     function(){ return Promise.resolve({ok:true}); },
    finish_intro:         function(){ S.intro_done = true; S.servers = SERVERS.slice(); return Promise.resolve(S); },
    check_conflicts:      function(){ return Promise.resolve({conflicts:[]}); },
    refresh_subscription: function(){ S.servers = SERVERS.slice(); return Promise.resolve({added:S.servers.length, state:S}); },
    add_links:            function(){ S.servers = SERVERS.slice(); return Promise.resolve({added:S.servers.length, state:S}); },
    import_subscription:  function(){ S.servers = SERVERS.slice(); return Promise.resolve({added:S.servers.length, state:S}); },
    ping_all:             function(){
                            S.servers.forEach(function(_, i){
                              setTimeout(function(){ window.__pushPing(i, 40 + Math.round(Math.random()*220)); },
                                         300 + Math.random()*900); });
                            return Promise.resolve({ok:true}); },
    connect:              function(i){
                            S.selected = i; S.connected = true;
                            if(S.settings.tun_mode) S.tun_active = true;
                            clearInterval(speedTimer);
                            speedTimer = setInterval(function(){
                              if(!S.connected) return;
                              var d = 400000 + Math.random()*2600000, u = 40000 + Math.random()*260000;
                              S.speed.total_down += d; S.speed.total_up += u;
                              window.__pushSpeed(u, d, S.speed.total_up, S.speed.total_down);
                            }, 1000);
                            window.__pushLog("[core] \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u043e \u044f\u0434\u0440\u043e (\u0434\u0435\u043c\u043e\u043d\u0441\u0442\u0440\u0430\u0446\u0438\u044f)");
                            window.__pushLog("[core] \u0441\u0435\u0440\u0432\u0435\u0440: " + S.servers[i].name);
                            return new Promise(function(res){ setTimeout(function(){ res(wrap()); }, 1200); }); },
    disconnect:           function(){ S.connected = false; S.tun_active = false;
                            clearInterval(speedTimer);
                            window.__pushLog("[core] \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u043e");
                            return Promise.resolve(wrap()); }
  }};
  window.dispatchEvent(new Event("pywebviewready"));
})();
</script>
"""

html = open(SRC, encoding="utf-8").read()
assert "</body>" in html

# Демо может уйти кому угодно, поэтому личный аккаунт из ссылок убираем —
# в боевой сборке он остаётся как был, до отдельного решения.
html = html.replace("https://t.me/mimidevil", "https://t.me/mackkill")

html = html.replace("</body>", MOCK + "</body>")
os.makedirs(OUT_DIR, exist_ok=True)
open(OUT, "w", encoding="utf-8").write(html)
print("демо собрано:", OUT)
print("размер: %.1f КБ" % (len(html.encode("utf-8")) / 1024))

утечки = [b for b in ("mimidevil", "jh8wrLwoGPbVAs0",
                      "DanDev", "user_8141998456") if b in html]
print("личные данные в демо:", утечки if утечки else "нет")
