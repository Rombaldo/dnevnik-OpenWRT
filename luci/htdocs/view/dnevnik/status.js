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
	logout_restore: rpc.declare({ object: 'luci.dnevnik', method: 'logout_restore' })
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
		return Promise.all([ call.status(), call.log('sync') ]);
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
		poll.add(function() { return self.poll(); }, 5);
		return node;
	}
});
