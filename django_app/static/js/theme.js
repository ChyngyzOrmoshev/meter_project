/* Переключатель светлой/тёмной темы.
   Выбор хранится в localStorage; по умолчанию светлая. Атрибут ставится сразу
   (скрипт в <head>), чтобы страница не мигала. Кнопки: .theme-toggle. */
(function () {
    'use strict';
    var KEY = 'theme';

    function stored() {
        try { var v = localStorage.getItem(KEY); return v === 'dark' || v === 'light' ? v : null; }
        catch (e) { return null; }
    }
    function apply(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
        var dark = theme === 'dark';
        document.querySelectorAll('.theme-toggle').forEach(function (b) {
            b.setAttribute('aria-pressed', dark ? 'true' : 'false');
            b.setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
            b.title = dark ? 'Светлая тема' : 'Тёмная тема';
        });
    }

    apply(stored() || 'light');

    document.addEventListener('DOMContentLoaded', function () {
        apply(document.documentElement.getAttribute('data-bs-theme') || 'light');
        document.querySelectorAll('.theme-toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
                try { localStorage.setItem(KEY, next); } catch (e) { /* приватный режим: просто не запоминаем */ }
                apply(next);
                document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next } }));
            });
        });
    });

    // Выбор в соседней вкладке применяется сразу
    window.addEventListener('storage', function (e) {
        if (e.key === KEY && (e.newValue === 'dark' || e.newValue === 'light')) {
            apply(e.newValue);
            document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: e.newValue } }));
        }
    });
})();
