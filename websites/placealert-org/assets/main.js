/* ============================================================
   Place Alert Labs — Shared JavaScript
   Extracted from app.blade.php layout
   ============================================================ */

// ── AOS Initialisation ──────────────────────────────────────
if (typeof AOS !== 'undefined') {
    AOS.init({ duration: 800, easing: 'ease-in-out', once: true });
}

// ── Bootstrap Icons initialisation ─────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const allIcons = document.querySelectorAll('i[class*="bi-"]');
    allIcons.forEach(icon => {
        if (!icon.style.fontFamily) icon.style.fontFamily = '"bootstrap-icons"';
        if (!icon.style.fontWeight) icon.style.fontWeight = 'normal';
    });
});

// ── Smooth cross-page anchor scrolling ───────────────────────
// Dropdown links point at /page.html#section. A normal hash link makes the
// browser jump there instantly on load, before any animation can run. So we
// strip the hash before navigating, remember the target, and slide to it
// smoothly once the destination page has actually loaded.
document.addEventListener('DOMContentLoaded', function () {
    const SCROLL_KEY = 'pals-scroll-target';

    document.addEventListener('click', function (e) {
        const link = e.target.closest('a[href*="#"]');
        if (!link) return;
        const url = new URL(link.href, window.location.href);
        if (!url.hash || url.hash === '#') return;

        const samePage = url.pathname === window.location.pathname;
        if (samePage) {
            const target = document.querySelector(url.hash);
            if (target) {
                e.preventDefault();
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                const navMenu = document.getElementById('nav-menu');
                if (navMenu?.classList.contains('active')) {
                    navMenu.classList.remove('active');
                    document.getElementById('mobile-menu-toggle')?.querySelector('i')?.classList.replace('bi-x-lg', 'bi-list');
                }
            }
            return;
        }

        e.preventDefault();
        sessionStorage.setItem(SCROLL_KEY, url.hash);
        window.location.href = url.pathname + url.search;
    });

    const pendingHash = sessionStorage.getItem(SCROLL_KEY);
    if (pendingHash) {
        sessionStorage.removeItem(SCROLL_KEY);
        const target = document.querySelector(pendingHash);
        if (target) {
            requestAnimationFrame(function () {
                setTimeout(function () {
                    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }, 120);
            });
        }
    }
});

// ── Mobile menu ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const toggle   = document.getElementById('mobile-menu-toggle');
    const navMenu  = document.getElementById('nav-menu');
    if (!toggle || !navMenu) return;

    toggle.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        navMenu.classList.toggle('active');
        const icon = toggle.querySelector('i');
        icon.classList.toggle('bi-list',   !navMenu.classList.contains('active'));
        icon.classList.toggle('bi-x-lg',   navMenu.classList.contains('active'));
    });

    document.addEventListener('click', function (e) {
        if (!navMenu.contains(e.target) && !toggle.contains(e.target)) {
            navMenu.classList.remove('active');
            const icon = toggle.querySelector('i');
            icon.classList.remove('bi-x-lg');
            icon.classList.add('bi-list');
        }
    });

    window.addEventListener('resize', function () {
        if (window.innerWidth > 768) {
            navMenu.classList.remove('active');
            const icon = toggle.querySelector('i');
            icon.classList.remove('bi-x-lg');
            icon.classList.add('bi-list');
        }
    });
});

// ── Active nav link detection ───────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
    document.querySelectorAll('.nav-link').forEach(link => {
        const href = link.getAttribute('href');
        if (href === currentPath || (currentPath === '/' && href === '/') ||
            (currentPath !== '/' && href !== '/' && href && currentPath.startsWith(href.replace('.html', '')))) {
            link.closest('.nav-item')?.classList.add('active');
        }
    });
    // Also check dropdown items
    document.querySelectorAll('.dropdown-item').forEach(item => {
        const href = item.getAttribute('href');
        if (href && href === currentPath) {
            item.closest('.nav-item')?.classList.add('active');
        }
    });
});

// ── Dropdown handling ──────────────────────────────────────
// Retired: nav links now go straight to their page, no submenu click required.

// ── Scroll-responsive header ────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const header = document.querySelector('.header');
    if (!header) return;
    window.addEventListener('scroll', function () {
        header.classList.toggle('scrolled', window.pageYOffset > 100);
    });
});

// ── Intersection observer for fade-in ───────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => { if (entry.isIntersecting) entry.target.classList.add('visible'); });
    }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });
    document.querySelectorAll('.fade-in').forEach(el => observer.observe(el));
});

// ── Smooth scrolling for anchor links ───────────────────────
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const target = document.querySelector(this.getAttribute('href'));
            if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });
});

// ── WhatsApp chat button ─────────────────────────────────────
function openWhatsApp() {
    const phone   = '+263782319736';
    const message = 'Hello! I would like to know more about Place Alert Labs research and services.';
    window.open(`https://wa.me/${phone}?text=${encodeURIComponent(message)}`, '_blank');
}

// ── Button ripple effect ─────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.btn').forEach(btn => {
        btn.addEventListener('click', function (e) {
            const ripple = document.createElement('span');
            const rect   = this.getBoundingClientRect();
            const size   = Math.max(rect.width, rect.height);
            ripple.style.cssText = `position:absolute;width:${size}px;height:${size}px;left:${e.clientX - rect.left - size/2}px;top:${e.clientY - rect.top - size/2}px;background:rgba(255,255,255,.3);border-radius:50%;transform:scale(0);animation:ripple .6s linear;pointer-events:none;`;
            this.appendChild(ripple);
            setTimeout(() => ripple.remove(), 600);
        });
    });
    const rippleStyle = document.createElement('style');
    rippleStyle.textContent = '@keyframes ripple{to{transform:scale(4);opacity:0;}}';
    document.head.appendChild(rippleStyle);
});

// ── Hero slider (for home page) ─────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const slides = document.querySelectorAll('.hero-slide');
    const dots   = document.querySelectorAll('.dot');
    if (!slides.length) return;
    let currentSlide = 0, slideInterval;

    function showSlide(index) {
        slides.forEach(s => s.classList.remove('active'));
        dots.forEach(d => d.classList.remove('active'));
        slides[index].classList.add('active');
        if (dots[index]) dots[index].classList.add('active');
        currentSlide = index;
    }
    function nextSlide() { showSlide((currentSlide + 1) % slides.length); }
    function startSlider() { slideInterval = setInterval(nextSlide, 10000); }
    function stopSlider()  { clearInterval(slideInterval); }

    dots.forEach((dot, i) => dot.addEventListener('click', () => { showSlide(i); stopSlider(); startSlider(); }));
    const heroSection = document.querySelector('.aas-hero-section');
    if (heroSection) {
        heroSection.addEventListener('mouseenter', stopSlider);
        heroSection.addEventListener('mouseleave', startSlider);
        // Touch/swipe
        let startX = 0;
        heroSection.addEventListener('touchstart', e => { startX = e.touches[0].clientX; });
        heroSection.addEventListener('touchend', e => {
            const diff = startX - e.changedTouches[0].clientX;
            if (Math.abs(diff) > 50) {
                if (diff > 0) nextSlide();
                else showSlide(currentSlide === 0 ? slides.length - 1 : currentSlide - 1);
                stopSlider(); startSlider();
            }
        });
    }
    startSlider();
});

// ── LinkedIn news feed ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const feeds = document.querySelectorAll('[data-news-feed]');
    if (!feeds.length) return;

    function createElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
    }

    function appendTags(container, tags) {
        const tagList = createElement('div', 'news-tags');
        (Array.isArray(tags) ? tags : []).slice(0, 5).forEach(tag => {
            tagList.appendChild(createElement('span', 'tag', tag));
        });
        if (tagList.children.length) container.appendChild(tagList);
    }

    function createLink(item, className) {
        const link = createElement('a', className, 'Read More on LinkedIn \u2192');
        link.href = item.url || 'https://www.linkedin.com/company/place-alert-labs/posts/?feedView=all';
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        return link;
    }

    function createHomeCard(item) {
        const card = createElement('div', 'linkedin-news-item');
        const icon = createElement('div', 'linkedin-icon');
        icon.innerHTML = '<i class="bi bi-linkedin"></i>';
        const content = createElement('div', 'linkedin-content');
        const meta = createElement('div', 'news-meta');
        meta.appendChild(createElement('span', 'news-date', item.date || 'Recent'));
        meta.appendChild(createElement('span', 'linkedin-category', item.category || 'UPDATE'));
        const title = createElement('h3');
        title.appendChild(createElement('strong', '', item.title || 'Place Alert Labs update'));
        content.append(meta, title, createElement('p', '', item.description || ''));
        appendTags(content, item.tags);
        content.appendChild(createLink(item, 'linkedin-link'));
        card.append(icon, content);
        return card;
    }

    function createArchiveCard(item, index) {
        const card = createElement('div', 'news-card');
        card.dataset.aos = 'fade-up';
        card.dataset.aosDelay = String((index + 1) * 100);
        const icon = createElement('div', 'news-icon');
        icon.innerHTML = '<i class="bi bi-linkedin"></i>';
        const body = createElement('div', 'news-body');
        const meta = createElement('div', 'news-meta');
        meta.appendChild(createElement('span', 'news-date', item.date || 'Recent'));
        meta.appendChild(createElement('span', 'news-category', item.category || 'UPDATE'));
        body.append(
            meta,
            createElement('h3', 'news-title', item.title || 'Place Alert Labs update'),
            createElement('p', 'news-excerpt', item.description || '')
        );
        appendTags(body, item.tags);
        body.appendChild(createLink(item, 'news-link'));
        card.append(icon, body);
        return card;
    }

    fetch('/data/news.json', { cache: 'no-store', headers: { Accept: 'application/json' } })
        .then(response => {
            if (!response.ok) throw new Error(`News feed returned ${response.status}`);
            return response.json();
        })
        .then(payload => {
            if (!payload || !Array.isArray(payload.items) || !payload.items.length) {
                throw new Error('News feed is empty');
            }
            feeds.forEach(feed => {
                const limit = Number.parseInt(feed.dataset.newsLimit || '', 10);
                const items = Number.isFinite(limit) ? payload.items.slice(0, limit) : payload.items;
                const fragment = document.createDocumentFragment();
                items.forEach((item, index) => {
                    fragment.appendChild(
                        feed.dataset.newsLayout === 'home'
                            ? createHomeCard(item)
                            : createArchiveCard(item, index)
                    );
                });
                feed.replaceChildren(fragment);
            });

            const updated = payload.updated_at ? new Date(payload.updated_at) : null;
            const statusText = updated && !Number.isNaN(updated.getTime())
                ? `LinkedIn feed last refreshed ${updated.toLocaleString()}.`
                : 'Showing the latest LinkedIn updates.';
            document.querySelectorAll('[data-news-status]').forEach(status => {
                status.textContent = statusText;
            });
            if (typeof AOS !== 'undefined') AOS.refreshHard();
        })
        .catch(error => {
            console.warn('Using embedded news fallback:', error.message);
        });
});
