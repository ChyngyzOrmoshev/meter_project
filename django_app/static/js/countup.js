/*
 * <count-up> — анимированный счётчик и каскадное появление карточек.
 *
 * Использование:
 *   <count-up value="42469" prefix="$" decimals="0">42 469</count-up>
 *   <count-up value="93.5" suffix="%" decimals="1" duration="1400" start-ratio="0.4"></count-up>
 *
 * Атрибуты: value (обязателен), prefix, suffix, decimals (0), duration (мс, 1300),
 *           start-ratio (доля итога, с которой стартует счёт, 0), locale (ru-RU).
 * Текст внутри тега — запасной вариант без JS; в работе заменяется отформатированным числом.
 *
 * Карточки с атрибутом data-reveal появляются каскадом (opacity, сдвиг, blur).
 * Счётчик внутри такой карточки стартует вместе с ней. Всё срабатывает один раз,
 * когда блок попадает в видимую область. При prefers-reduced-motion сразу показывается итог.
 */
(function () {
    'use strict';

    var STAGGER_MS = 90;
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var hasIO = 'IntersectionObserver' in window;

    // CSS прячет карточки только когда JS точно работает
    document.documentElement.classList.add('js');

    function easeOutExpo(t) {
        return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t);
    }

    class CountUp extends HTMLElement {
        connectedCallback() {
            if (this._init) return;
            this._init = true;

            var value = parseFloat(String(this.getAttribute('value')).replace(',', '.'));
            if (!isFinite(value)) return;  // нет корректного значения - оставляем текст как есть
            this._value = value;
            this._decimals = Math.max(0, parseInt(this.getAttribute('decimals'), 10) || 0);
            this._duration = parseInt(this.getAttribute('duration'), 10) || 1300;
            this._startRatio = Math.min(1, Math.max(0, parseFloat(this.getAttribute('start-ratio')) || 0));
            this._prefix = this.getAttribute('prefix') || '';
            this._suffix = this.getAttribute('suffix') || '';
            this._fmt = new Intl.NumberFormat(this.getAttribute('locale') || 'ru-RU', {
                minimumFractionDigits: this._decimals,
                maximumFractionDigits: this._decimals,
            });

            var final = this._format(value);
            this.setAttribute('aria-label', final);
            this.style.fontVariantNumeric = 'tabular-nums';

            if (reduceMotion || !hasIO) {
                this.textContent = final;
                return;
            }

            this.textContent = this._format(value * this._startRatio);

            var card = this.closest('[data-reveal]');
            if (card && !card.classList.contains('is-visible')) {
                // стартуем вместе с карточкой
                card.addEventListener('reveal', this._run.bind(this), { once: true });
            } else {
                var io = new IntersectionObserver(function (entries) {
                    if (entries.some(function (e) { return e.isIntersecting; })) {
                        io.disconnect();
                        this._run();
                    }
                }.bind(this), { threshold: 0.3 });
                io.observe(this);
            }
        }

        _format(n) {
            return this._prefix + this._fmt.format(n) + this._suffix;
        }

        _run() {
            if (this._started) return;
            this._started = true;
            var from = this._value * this._startRatio;
            var to = this._value;
            var duration = this._duration;
            var begin = performance.now();
            var self = this;
            requestAnimationFrame(function frame(now) {
                var p = Math.min((now - begin) / duration, 1);
                self.textContent = self._format(from + (to - from) * easeOutExpo(p));
                if (p < 1) requestAnimationFrame(frame);
                else self.textContent = self._format(to);
            });
        }
    }

    if (!customElements.get('count-up')) customElements.define('count-up', CountUp);

    // ----- каскадное появление карточек -----
    function showNow(card) {
        card.classList.add('is-visible', 'reveal-done');
        card.dispatchEvent(new CustomEvent('reveal'));
    }

    function initReveal() {
        var cards = document.querySelectorAll('[data-reveal]:not(.is-visible)');
        if (!cards.length) return;
        if (reduceMotion || !hasIO) {
            cards.forEach(showNow);
            return;
        }
        var io = new IntersectionObserver(function (entries) {
            // порядок в пачке = порядок каскада
            entries.filter(function (e) { return e.isIntersecting; }).forEach(function (e, i) {
                var card = e.target;
                io.unobserve(card);
                var delay = i * STAGGER_MS;
                card.style.transitionDelay = delay + 'ms';
                card.classList.add('is-visible');
                setTimeout(function () {
                    card.dispatchEvent(new CustomEvent('reveal'));
                }, delay);
                // после появления возвращаем быстрые переходы для hover
                setTimeout(function () {
                    card.style.transitionDelay = '';
                    card.classList.add('reveal-done');
                }, delay + 520);
            });
        }, { threshold: 0.2 });
        cards.forEach(function (c) { io.observe(c); });
    }

    // для карточек, добавленных на страницу позже (fetch + innerHTML)
    window.initReveal = initReveal;

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initReveal);
    else initReveal();
})();
