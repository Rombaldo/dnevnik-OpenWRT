'use strict';
'require view';
'require rpc';
'require ui';
'require poll';

/* Страница «Службы → Дневник». Серверная часть: /usr/libexec/rpcd/luci.dnevnik */

var call = {
	status: rpc.declare({ object: 'luci.dnevnik', method: 'status' }),
	log: rpc.declare({ object: 'luci.dnevnik', method: 'log', params: [ 'which' ] }),
	refresh: rpc.declare({ object: 'luci.dnevnik', method: 'refresh' }),
	check_update: rpc.declare({ object: 'luci.dnevnik', method: 'check_update' }),
	update: rpc.declare({ object: 'luci.dnevnik', method: 'update' }),
	set_config: rpc.declare({ object: 'luci.dnevnik', method: 'set_config', params: [ 'every', 'from', 'to', 'name' ] }),
	logout_begin: rpc.declare({ object: 'luci.dnevnik', method: 'logout_begin' }),
	logout_confirm: rpc.declare({ object: 'luci.dnevnik', method: 'logout_confirm', params: [ 'token' ] }),
	logout_restore: rpc.declare({ object: 'luci.dnevnik', method: 'logout_restore' }),
	family_set: rpc.declare({ object: 'luci.dnevnik', method: 'family_set', params: [ 'password', 'hint' ] }),
	family_off: rpc.declare({ object: 'luci.dnevnik', method: 'family_off' }),
	remote_info: rpc.declare({ object: 'luci.dnevnik', method: 'remote_info' }),
	doctor: rpc.declare({ object: 'luci.dnevnik', method: 'doctor' }),
	devices_list: rpc.declare({ object: 'luci.dnevnik', method: 'devices_list' }),
	device_remove: rpc.declare({ object: 'luci.dnevnik', method: 'device_remove', params: [ 'id' ] }),
	logins_clear: rpc.declare({ object: 'luci.dnevnik', method: 'logins_clear' })
};

var STYLE =
	'.dn{max-width:780px;--dn-line:rgba(128,128,128,.35);--dn-soft:rgba(128,128,128,.12);--dn-muted:#777;--dn-ok:#2e7d32;--dn-warn:#b26a00;--dn-bad:#c62828}' +
	'.dn-card{border:1px solid var(--dn-line);border-radius:12px;padding:16px 20px;margin-bottom:14px}' +
	'.dn-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}' +
	'.dn-title{font-size:18px;font-weight:600;margin:0}' +
	'.dn-pill{font-size:12px;padding:3px 10px;border-radius:8px;white-space:nowrap;background:var(--dn-soft)}' +
	'.dn-pill.ok{color:var(--dn-ok)}.dn-pill.warn{color:var(--dn-warn)}.dn-pill.bad{color:var(--dn-bad)}' +
	'.dn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 20px}' +
	'.dn-k{color:var(--dn-muted);font-size:12px;margin:0}.dn-v{font-size:14px;margin:0;overflow-wrap:anywhere}' +
	'.dn-btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}' +
	'.dn-note{font-size:13px;padding:10px 12px;border-radius:8px;background:var(--dn-soft);margin:12px 0 0}' +
	'.dn-note.ok{color:var(--dn-ok)}.dn-note.warn{color:var(--dn-warn)}.dn-note.bad{color:var(--dn-bad)}' +
	'.dn-form{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr);gap:10px 12px;align-items:center;font-size:14px}' +
	'.dn-form select,.dn-form input{max-width:240px;min-width:0}' +
	'@media(max-width:600px){.dn-form{grid-template-columns:minmax(0,1fr)}.dn-form select,.dn-form input{max-width:none;width:100%}.dn-head{flex-wrap:wrap}}' +
	'.dn-pform{display:grid;grid-template-columns:minmax(150px,220px) minmax(0,1fr);gap:10px 12px;align-items:center;font-size:14px;margin-top:12px}' +
	'.dn-pform input{max-width:320px;min-width:0}' +
	'@media(max-width:600px){.dn-pform{grid-template-columns:minmax(0,1fr)}.dn-pform input{max-width:none;width:100%}}' +
	'.dn-chk{padding:7px 0;border-top:1px solid var(--dn-line);font-size:13px;overflow-wrap:anywhere}.dn-chk:first-child{border-top:0}' +
	'.dn-chk.ok{color:var(--dn-ok)}.dn-chk.warn{color:var(--dn-warn)}.dn-chk.fail{color:var(--dn-bad)}.dn-chk.info{color:var(--dn-muted)}' +
	'.dn-fix{color:inherit;opacity:.9;margin:2px 0 0 18px;font-size:12px}' +
	'.dn-invite{width:100%;min-height:190px;font-size:13px;margin-top:10px}' +
	'.dn-code{font:12px monospace;background:var(--dn-soft);padding:6px 8px;border-radius:6px;overflow-wrap:anywhere;margin:6px 0 0}' +
	'.dn-hint{color:var(--dn-muted);font-size:12px;margin:8px 0 0}' +
	'.dn-log{max-height:260px;overflow:auto;font:12px monospace;white-space:pre-wrap;background:var(--dn-soft);padding:8px;border-radius:8px;margin:8px 0 0}';

var MONTHS = [ 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря' ];
var EVERY = [ 5, 10, 15, 20, 30, 60 ];

function two(n) { return (n < 10 ? '0' : '') + n; }
function clock(d) { return two(d.getHours()) + ':' + two(d.getMinutes()); }
function whenText(iso) {
	if (!iso) return 'ещё не обновлялось';
	var d = new Date(iso);
	if (isNaN(d.getTime())) return '—';
	return d.getDate() + ' ' + MONTHS[d.getMonth()] + ', ' + clock(d);
}
function nextText(st) {
	if (st.nextIn == null) return 'по расписанию ничего не запланировано';
	var d = new Date(Date.now() + st.nextIn * 1000), mins = Math.max(1, Math.round(st.nextIn / 60));
	return 'в ' + clock(d) + (mins < 60 ? ' (через ' + mins + ' мин)' : '');
}
function hourOptions(from, to, selected, suffix) {
	var out = [];
	for (var h = from; h <= to; h++)
		out.push(E('option', Object.assign({ value: h }, h === selected ? { selected: 'selected' } : {}), h + suffix));
	return out;
}

return view.extend({
	handleSave: null,
	handleSaveApply: null,
	handleReset: null,

	load: function() {
		return Promise.all([ call.status(), call.log('sync'), call.remote_info(), call.devices_list() ]);
	},

	inviteText: function(url) {
		return 'Дневник школьника: ' + url + '\n\n' +
			'1. Откройте ссылку в браузере на телефоне или компьютере. Ничего устанавливать не нужно.\n' +
			'2. Введите пароль семьи (его сообщит тот, кто настраивал дневник).\n' +
			'3. Чтобы дневник был как приложение. iPhone: Safari → «Поделиться» → «На экран Домой». Android: Chrome → ⋮ → «Установить приложение» или «Добавить на главный экран».\n\n' +
			'Если страница не открылась, обновите её ещё раз: иногда нужно 2–3 попытки.';
	},

	ago: function(ts, now) {
		var s = Math.max(0, (now || Math.floor(Date.now() / 1000)) - ts);
		if (s < 90) return 'только что';
		if (s < 3600) return Math.round(s / 60) + ' мин назад';
		if (s < 86400) return Math.round(s / 3600) + ' ч назад';
		return whenText(new Date(ts * 1000).toISOString());
	},

	paintDevices: function(d) {
		var self = this, r = this.refs, now = d && d.now;
		this.devs = d || {};
		r.devbox.textContent = '';
		if (!d || !d.ok) { r.devbox.appendChild(E('p', { 'class': 'dn-hint' }, 'Список недоступен. Обновите программу.')); return; }
		var row = function(cls, head, sub, extra) {
			var n = E('div', { 'class': 'dn-chk ' + cls }, [ E('b', {}, head), sub ? E('div', { 'class': 'dn-fix', style: 'margin-left:0' }, sub) : '' ]);
			if (extra) n.appendChild(extra);
			return n;
		};
		var where = { home: 'из дома', internet: 'из интернета', tailscale: 'через Tailscale', unknown: 'откуда — неизвестно' };
		r.devpill.className = 'dn-pill ' + (d.failed24h ? 'warn' : 'ok');
		r.devpill.textContent = d.failed24h ? 'Неверных попыток за сутки: ' + d.failed24h : 'Всё спокойно';
		if (!d.family) r.devbox.appendChild(E('div', { 'class': 'dn-note warn' }, 'Пароль семьи не задан: входы не записываются, различить устройства нельзя.'));
		r.devbox.appendChild(E('p', { 'class': 'dn-k' }, 'Подключены к уведомлениям'));
		if (!(d.subs || []).length) r.devbox.appendChild(E('p', { 'class': 'dn-hint' }, 'Пока ни одного устройства (включается на телефоне: ⚙ → «Уведомления»).'));
		(d.subs || []).forEach(function(s) {
			var btn = E('button', { 'class': 'cbi-button cbi-button-negative', style: 'margin-top:6px', click: function() { return self.doRemoveDevice(s); } }, 'Отключить');
			r.devbox.appendChild(row(s.fail ? 'warn' : 'ok', (s.name || s.label) + (s.name ? ' · ' + s.label : ''),
				s.service + ', подключено ' + whenText(new Date((s.created || 0) * 1000).toISOString()) + (s.pending ? ', в очереди: ' + s.pending : '') + (s.fail ? ', сбоев подряд: ' + s.fail : ''), btn));
		});
		r.devbox.appendChild(E('p', { 'class': 'dn-k', style: 'margin-top:14px' }, 'Кто открывал дневник (с последней перезагрузки роутера)'));
		if (!(d.seen || []).length) r.devbox.appendChild(E('p', { 'class': 'dn-hint' }, 'Пока никого.'));
		(d.seen || []).forEach(function(s) {
			r.devbox.appendChild(row('info', s.dev, where[s.where] + ', последний раз ' + self.ago(s.ts, now)));
		});
		r.devbox.appendChild(E('p', { 'class': 'dn-k', style: 'margin-top:14px' }, 'Журнал входов по паролю семьи'));
		if (!(d.logins || []).length) r.devbox.appendChild(E('p', { 'class': 'dn-hint' }, 'Пока записей нет.'));
		(d.logins || []).forEach(function(e) {
			r.devbox.appendChild(row(e.ok ? 'ok' : 'fail', (e.ok ? '✓ Вошёл: ' : '✗ Неверный пароль' + (e.n > 1 ? ' (×' + e.n + ')' : '') + ': ') + e.dev,
				where[e.where] + ', ' + self.ago(e.ts, now)));
		});
	},

	doDevices: function() {
		var self = this;
		return call.devices_list().then(function(d) { self.paintDevices(d); });
	},

	doRemoveDevice: function(s) {
		var self = this;
		ui.showModal('Отключить устройство?', [
			E('p', 'Устройство «' + (s.name || s.label) + '» перестанет получать уведомления. Включить их снова можно на самом телефоне (⚙ → «Уведомления»).'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button', click: ui.hideModal }, 'Отмена'), ' ',
				E('button', { 'class': 'cbi-button cbi-button-negative', click: function() {
					ui.hideModal();
					return call.device_remove(s.id).then(function(res) {
						self.say(self.refs.devnote, res.ok ? 'ok' : 'warn', res.message);
						return self.doDevices();
					});
				} }, 'Отключить')
			])
		]);
	},

	doClearLogins: function() {
		var self = this;
		ui.showModal('Очистить журнал входов?', [
			E('p', 'Журнал входов и список «кто открывал» будут стёрты. Подключённые уведомления это не затронет.'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button', click: ui.hideModal }, 'Отмена'), ' ',
				E('button', { 'class': 'cbi-button cbi-button-negative', click: function() {
					ui.hideModal();
					return call.logins_clear().then(function(res) {
						self.say(self.refs.devnote, res.ok ? 'ok' : 'bad', res.message);
						return self.doDevices();
					});
				} }, 'Очистить')
			])
		]);
	},

	paintRemote: function(info) {
		var r = this.refs, on = !!(info && info.funnel), installed = !!(info && info.installed);
		this.remote = info || {};
		r.rpill.className = 'dn-pill ' + (on ? 'ok' : 'warn');
		r.rpill.textContent = on ? 'Доступно из интернета' : (installed ? 'Не включено' : 'Только дома');
		r.rurl.textContent = on ? info.url : '—';
		r.rurl.href = on ? info.url : '#';
		r.rstate.textContent = !installed ? 'Tailscale не установлен' : 'Tailscale: ' + (info.state || 'нет ответа') +
			(info.proxy ? '; через прокси ' + info.proxy + (info.proxyAlive ? '' : ' (прокси не отвечает!)') : '');
		r.rhow.style.display = on ? 'none' : '';
		r.rcmd.textContent = installed ? 'sh ' + (info.command || '/root/dnevnik/router/remote.sh')
			: 'wget -qO- https://raw.githubusercontent.com/Rombaldo/dnevnik-OpenWRT/main/router/install.sh | sh -s -- --remote';
		r.rinvite.disabled = !on;
		if (!on) r.invite.style.display = 'none';
	},

	doInvite: function() {
		var r = this.refs;
		if (!this.remote || !this.remote.funnel) return;
		r.invite.value = this.inviteText(this.remote.url);
		r.invite.style.display = '';
		r.invite.focus();
		r.invite.select();
		try { document.execCommand('copy'); this.say(r.rnote, 'ok', 'Текст приглашения выделен и скопирован: вставьте его в сообщение семье.'); }
		catch (e) { this.say(r.rnote, 'ok', 'Текст приглашения выделен: скопируйте его (Ctrl+C) и вставьте в сообщение семье.'); }
	},

	doDoctor: function() {
		var self = this, r = this.refs;
		r.rdoc.disabled = true;
		self.say(r.rnote, '', 'Проверяю (до 20 секунд)…');
		return call.doctor().then(function(res) {
			r.rdoc.disabled = false;
			r.chk.textContent = '';
			if (!res || !res.ok) return self.say(r.rnote, 'bad', (res && res.message) || 'Проверка не удалась.');
			var mark = { ok: '✓', warn: '!', fail: '✗', info: '·' };
			(res.results || []).forEach(function(c) {
				var row = E('div', { 'class': 'dn-chk ' + c.status }, mark[c.status] + ' ' + c.title + (c.detail ? ': ' + c.detail : ''));
				if (c.fix && (c.status === 'warn' || c.status === 'fail')) row.appendChild(E('div', { 'class': 'dn-fix' }, 'Что делать: ' + c.fix));
				r.chk.appendChild(row);
			});
			var v = res.summary && res.summary.verdict;
			self.say(r.rnote, v === 'ok' ? 'ok' : v === 'notes' ? 'warn' : 'bad',
				v === 'ok' ? 'Всё работает.' : v === 'notes' ? 'Работает, но есть замечания (отмечены «!»).' : 'Есть проблемы (отмечены «✗»): что делать, написано под каждой.');
			return self.refreshRemote();
		});
	},

	refreshRemote: function() {
		var self = this;
		return call.remote_info().then(function(i) { self.paintRemote(i); });
	},

	pill: function(st) {
		if (st.updating) return [ 'warn', 'Обновляется программа…' ];
		if (st.syncing) return [ 'warn', 'Обновляются данные…' ];
		if (st.needLogin) return [ 'bad', 'Нужен вход' ];
		if (st.error) return [ 'warn', 'Не удалось обновить' ];
		return [ 'ok', 'Работает' ];
	},

	paint: function(st) {
		var r = this.refs, p = this.pill(st);
		this.st = st;
		r.pill.className = 'dn-pill ' + p[0];
		r.pill.textContent = p[1];
		r.version.textContent = st.version || '—';
		r.student.textContent = st.student ? st.student + (st.school ? ' (' + st.school + ')' : '') : '—';
		r.login.textContent = st.needLogin ? 'нужно войти (на странице дневника)' : 'выполнен';
		r.updated.textContent = whenText(st.updatedAt) + (st.days ? ', дней в расписании: ' + st.days : '');
		r.next.textContent = nextText(st);
		r.link.textContent = st.url;
		r.link.href = st.url;
		r.link2.textContent = st.urlIp;
		r.link2.href = st.urlIp || '#';
		r.link2.style.display = st.urlIp ? '' : 'none';
		r.refresh.disabled = !!st.syncing || !!st.updating;
		r.restore.style.display = st.canRestore ? '' : 'none';
		r.logout.disabled = !!st.needLogin;
		r.fpill.className = 'dn-pill ' + (st.family ? 'ok' : 'warn');
		r.fpill.textContent = st.family ? 'Пароль включён' : 'Пароль не задан';
		r.fstate.textContent = st.family
			? 'Дневник закрыт паролем' + (st.familyChanged ? ' (задан ' + whenText(new Date(st.familyChanged * 1000).toISOString()) + ')' : '') + '.'
			: 'Сейчас дневник открыт всем, кто до него доберётся. Если страницу можно открыть из интернета, обязательно задайте пароль.';
		r.fset.textContent = st.family ? 'Сменить пароль' : 'Задать пароль';
		r.foff.style.display = st.family ? '' : 'none';
		if (!this.hintFilled) { r.fhint.value = st.familyHint || ''; this.hintFilled = true; }       // подсказку подставляем один раз, чтобы не стирать то, что вы печатаете
	},

	poll: function() {
		var self = this;
		return Promise.all([ call.status(), call.log(this.refs.which.value) ]).then(function(res) {
			self.paint(res[0]);
			self.refs.log.textContent = (res[1] && res[1].text) || 'Журнал пока пуст.';
		});
	},

	say: function(node, cls, text) {
		node.className = 'dn-note ' + cls;
		node.textContent = text;
		node.style.display = text ? '' : 'none';
	},

	doRefresh: function() {
		var self = this;
		return call.refresh().then(function(r) {
			self.say(self.refs.note, r.started ? 'ok' : 'warn', r.message);
			return self.poll();
		});
	},

	doCheck: function() {
		var self = this, r = this.refs;
		self.say(r.updnote, '', 'Проверяю…');
		return call.check_update().then(function(res) {
			r.updbtn.style.display = 'none';
			if (!res.ok) return self.say(r.updnote, 'warn', res.message);
			if (!res.newer) return self.say(r.updnote, 'ok', 'Установлена последняя версия: ' + res.current + '.');
			self.say(r.updnote, 'warn', 'Есть новая версия ' + res.latest + ' (у вас ' + res.current + ').');
			r.updbtn.textContent = 'Обновить до ' + res.latest;
			r.updbtn.style.display = '';
		});
	},

	doUpdate: function() {
		var self = this;
		ui.showModal('Обновить программу?', [
			E('p', 'Роутер скачает новую версию с GitHub и поставит её. Вход и данные не трогаются. Страница дневника на минуту может не открываться.'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button', click: ui.hideModal }, 'Отмена'), ' ',
				E('button', { 'class': 'cbi-button cbi-button-action', click: function() {
					ui.hideModal();
					return call.update().then(function(r) {
						self.say(self.refs.updnote, r.ok ? 'ok' : 'bad', r.message);
						if (r.ok) { self.refs.which.value = 'update'; }
						return self.poll();
					});
				} }, 'Обновить')
			])
		]);
	},

	doSave: function() {
		var self = this, r = this.refs;
		return call.set_config(Number(r.every.value), Number(r.from.value), Number(r.to.value), r.name.value).then(function(res) {
			self.say(r.savenote, res.ok ? 'ok' : 'bad', res.message);
			return self.poll();
		});
	},

	doLogout: function() {
		var self = this;
		ui.showModal('Выйти из аккаунта?', [
			E('p', 'Дневник перестанет показываться на телефонах, придётся войти заново. Прежний вход сохранится на сутки, его можно будет вернуть. Выйти?'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button', click: ui.hideModal }, 'Отмена'), ' ',
				E('button', { 'class': 'cbi-button cbi-button-negative', click: function() {
					return call.logout_begin().then(function(b) {
						if (!b || !b.token) { ui.hideModal(); return self.say(self.refs.note, 'bad', 'Не получилось начать выход.'); }
						self.logoutStep2(b.token);
					});
				} }, 'Да, выйти')
			])
		]);
	},

	logoutStep2: function(token) {
		var self = this, go = E('button', { 'class': 'cbi-button cbi-button-negative', disabled: 'disabled', click: function() {
			return call.logout_confirm(token).then(function(r) {
				ui.hideModal();
				self.say(self.refs.note, r.ok ? 'ok' : 'bad', r.message);
				return self.poll();
			});
		} }, 'Да, выйти из аккаунта');
		setTimeout(function() { go.disabled = false; }, 3000);
		ui.showModal('Вы точно хотите выйти из этого аккаунта?', [
			E('p', 'Показывать дневник перестанет. Войти заново можно только по логину и паролю Дневник.ру (вход через Госуслуги делается на компьютере). Если нажали случайно, выберите «Нет, оставить как есть».'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button cbi-button-action', click: ui.hideModal }, 'Нет, оставить как есть'), ' ', go
			])
		]);
	},

	doFamilySet: function() {
		var self = this, r = this.refs, pw = r.fpw.value, again = r.fpw2.value;
		if (!pw.trim()) return self.say(r.fnote, 'bad', 'Введите пароль.');
		if (pw !== again) return self.say(r.fnote, 'bad', 'Пароли не совпадают.');
		return call.family_set(pw, r.fhint.value).then(function(res) {
			self.say(r.fnote, res.ok ? 'ok' : 'bad', res.message);
			if (res.ok) { r.fpw.value = ''; r.fpw2.value = ''; }
			return self.poll();
		});
	},

	doFamilyOff: function() {
		var self = this;
		ui.showModal('Отключить пароль?', [
			E('p', 'Дневник станет открыт всем, кто увидит страницу. Если она доступна из интернета, отключать пароль не стоит.'),
			E('div', { 'class': 'right' }, [
				E('button', { 'class': 'cbi-button', click: ui.hideModal }, 'Отмена'), ' ',
				E('button', { 'class': 'cbi-button cbi-button-negative', click: function() {
					ui.hideModal();
					return call.family_off().then(function(res) {
						self.say(self.refs.fnote, res.ok ? 'ok' : 'bad', res.message);
						return self.poll();
					});
				} }, 'Отключить пароль')
			])
		]);
	},

	doRestore: function() {
		var self = this;
		return call.logout_restore().then(function(r) {
			self.say(self.refs.note, r.ok ? 'ok' : 'bad', r.message);
			return self.poll();
		});
	},

	render: function(data) {
		var self = this, st = data[0], text = (data[1] && data[1].text) || 'Журнал пока пуст.', r = this.refs = {};
		var kv = function(key, node) { return E('div', {}, [ E('p', { 'class': 'dn-k' }, key), node ]); };
		var val = function(name) { return (r[name] = E('p', { 'class': 'dn-v' }, '—')); };

		r.pill = E('span', { 'class': 'dn-pill' }, '…');
		r.link = E('a', { href: '#', target: '_blank', rel: 'noopener' }, '');
		r.link2 = E('a', { href: '#', target: '_blank', rel: 'noopener' }, '');
		r.note = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.updnote = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.savenote = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.refresh = E('button', { 'class': 'cbi-button cbi-button-action', click: function() { return self.doRefresh(); } }, 'Обновить сейчас');
		r.check = E('button', { 'class': 'cbi-button', click: function() { return self.doCheck(); } }, 'Проверить новую версию');
		r.updbtn = E('button', { 'class': 'cbi-button cbi-button-apply', style: 'display:none', click: function() { return self.doUpdate(); } }, 'Обновить');
		r.logout = E('button', { 'class': 'cbi-button cbi-button-negative', click: function() { return self.doLogout(); } }, 'Выйти из аккаунта');
		r.restore = E('button', { 'class': 'cbi-button', style: 'display:none', click: function() { return self.doRestore(); } }, 'Вернуть прежний вход');
		r.open = E('a', { 'class': 'cbi-button cbi-button-action', href: st.url, target: '_blank', rel: 'noopener' }, 'Открыть дневник');
		r.fpill = E('span', { 'class': 'dn-pill' }, '…');
		r.fstate = E('p', { 'class': 'dn-v' }, '');
		r.fnote = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.fpw = E('input', { 'class': 'cbi-input-password', type: 'password', autocomplete: 'new-password', maxlength: 128 });
		r.fpw2 = E('input', { 'class': 'cbi-input-password', type: 'password', autocomplete: 'new-password', maxlength: 128 });
		r.fhint = E('input', { 'class': 'cbi-input-text', type: 'text', maxlength: 100, placeholder: 'например: имя собаки и год' });
		r.devpill = E('span', { 'class': 'dn-pill' }, '…');
		r.devbox = E('div', {});
		r.devnote = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.rpill = E('span', { 'class': 'dn-pill' }, '…');
		r.rurl = E('a', { href: '#', target: '_blank', rel: 'noopener' }, '—');
		r.rstate = E('p', { 'class': 'dn-v' }, '');
		r.rnote = E('div', { 'class': 'dn-note', style: 'display:none' });
		r.chk = E('div', { 'class': 'dn-chks' });
		r.rcmd = E('div', { 'class': 'dn-code' }, '');
		r.rhow = E('div', {}, [
			E('p', { 'class': 'dn-hint' }, 'Чтобы дневник открывался не только дома, выполните на роутере по SSH (нужен пароль семьи и бесплатный аккаунт Tailscale; команда сама скажет, что нажать в браузере):'),
			r.rcmd
		]);
		r.invite = E('textarea', { 'class': 'dn-invite cbi-input-textarea', readonly: 'readonly', style: 'display:none' });
		r.rdoc = E('button', { 'class': 'cbi-button cbi-button-action', click: function() { return self.doDoctor(); } }, 'Проверить всё');
		r.rinvite = E('button', { 'class': 'cbi-button', click: function() { return self.doInvite(); } }, 'Приглашение для семьи');
		r.fset = E('button', { 'class': 'cbi-button cbi-button-action', click: function() { return self.doFamilySet(); } }, 'Задать пароль');
		r.foff = E('button', { 'class': 'cbi-button cbi-button-negative', style: 'display:none', click: function() { return self.doFamilyOff(); } }, 'Отключить пароль');

		var linkBox = E('p', { 'class': 'dn-v' }, [ r.link, E('br'), r.link2 ]);

		r.every = E('select', { 'class': 'cbi-input-select' }, EVERY.map(function(n) {
			return E('option', Object.assign({ value: n }, n === st.every ? { selected: 'selected' } : {}), n === 60 ? 'раз в час' : 'каждые ' + n + ' минут');
		}));
		r.from = E('select', { 'class': 'cbi-input-select' }, hourOptions(0, 23, st.from, ':00'));
		r.to = E('select', { 'class': 'cbi-input-select' }, hourOptions(1, 24, st.to, ':00'));
		r.name = E('input', { 'class': 'cbi-input-text', type: 'text', maxlength: 40, value: st.name || '', placeholder: 'как на сайте' });
		r.which = E('select', { 'class': 'cbi-input-select', change: function() { return self.poll(); } }, [
			E('option', { value: 'sync' }, 'Данные с сайта'), E('option', { value: 'update' }, 'Обновление программы')
		]);
		r.log = E('pre', { 'class': 'dn-log' }, text);

		var node = E('div', { 'class': 'dn' }, [
			E('style', STYLE),
			E('h2', { name: 'content' }, 'Дневник'),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Состояние'), r.pill ]),
				E('div', { 'class': 'dn-grid' }, [
					kv('Версия программы', val('version')), kv('Ученик', val('student')), kv('Вход в Дневник.ру', val('login')),
					kv('Последнее обновление', val('updated')), kv('Следующее обновление', val('next')), kv('Адрес страницы', linkBox)
				]),
				E('div', { 'class': 'dn-btns' }, [ r.open, r.refresh, r.check, r.updbtn, r.logout, r.restore ]),
				r.note, r.updnote
			]),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Пароль семьи'), r.fpill ]),
				r.fstate,
				E('p', { 'class': 'dn-hint' }, 'Один общий пароль на всю семью. Каждое устройство вводит его один раз и запоминает на полгода. Старый пароль знать не нужно: забыли — просто задайте новый здесь, и все устройства войдут заново.'),
				E('div', { 'class': 'dn-pform' }, [
					E('label', {}, 'Новый пароль'), r.fpw,
					E('label', {}, 'Повторите пароль'), r.fpw2,
					E('label', {}, 'Подсказка (по желанию)'), r.fhint
				]),
				E('p', { 'class': 'dn-hint' }, 'Пароль может быть любым, даже из одного символа, но короткий легко подобрать, особенно если дневник открыт из интернета: лучше фраза из нескольких слов. Подсказку видит каждый на экране входа, поэтому сам пароль в ней писать нельзя.'),
				E('div', { 'class': 'dn-btns' }, [ r.fset, r.foff ]),
				r.fnote
			]),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Устройства и входы'), r.devpill ]),
				r.devbox,
				E('div', { 'class': 'dn-btns' }, [
					E('button', { 'class': 'cbi-button', click: function() { return self.doDevices(); } }, 'Обновить список'),
					E('button', { 'class': 'cbi-button cbi-button-negative', click: function() { return self.doClearLogins(); } }, 'Очистить журнал')
				]),
				r.devnote,
				E('p', { 'class': 'dn-hint' }, 'Устройства различаются по типу и браузеру (имя можно задать при включении уведомлений). Адреса не записываются; из интернета роутер видит только «из интернета», а не чей именно адрес.')
			]),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Доступ из интернета и проверка'), r.rpill ]),
				E('div', { 'class': 'dn-grid' }, [ kv('Адрес для семьи', E('p', { 'class': 'dn-v' }, [ r.rurl ])), kv('Состояние', r.rstate) ]),
				r.rhow,
				E('div', { 'class': 'dn-btns' }, [ r.rdoc, r.rinvite ]),
				r.rnote, r.invite, r.chk
			]),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Настройки') ]),
				E('div', { 'class': 'dn-form' }, [
					E('label', {}, 'Обновлять'), r.every,
					E('label', {}, 'Работать с'), r.from,
					E('label', {}, 'До'), r.to,
					E('label', {}, 'Имя в шапке'), r.name
				]),
				E('p', { 'class': 'dn-hint' }, 'Ночью на сайт не ходим. Чаще раза в 10 минут не стоит: сайт может начать ограничивать вход. Имя в шапке можно оставить пустым, тогда берётся имя ученика с сайта.'),
				E('div', { 'class': 'dn-btns' }, [ E('button', { 'class': 'cbi-button cbi-button-save', click: function() { return self.doSave(); } }, 'Сохранить') ]),
				r.savenote
			]),
			E('div', { 'class': 'dn-card' }, [
				E('div', { 'class': 'dn-head' }, [ E('p', { 'class': 'dn-title' }, 'Журнал'), r.which ]),
				r.log
			])
		]);

		this.paint(st);
		this.paintRemote(data[2]);
		this.paintDevices(data[3]);
		poll.add(function() { return self.poll(); }, 5);
		return node;
	}
});
