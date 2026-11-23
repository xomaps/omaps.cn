// 官网交互：移动端导航、数字滚动、滚动渐显、年份、头部收缩、悬浮联系栏
document.addEventListener('DOMContentLoaded', function () {
  // 头部导航：滚动后收缩为居中圆角卡片
  var header = document.querySelector('.site-header');
  if (header) {
    var onHeaderScroll = function () {
      header.classList.toggle('scrolled', window.scrollY > 30);
    };
    window.addEventListener('scroll', onHeaderScroll, { passive: true });
    onHeaderScroll();
  }

  // 右侧悬浮联系栏：回到顶部 + 移动端小程序码弹层
  var topBtn = document.getElementById('backToTop');
  if (topBtn) {
    var onTopScroll = function () {
      topBtn.classList.toggle('show', window.scrollY > 400);
    };
    window.addEventListener('scroll', onTopScroll, { passive: true });
    onTopScroll();
    topBtn.addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  var wechatBtn = document.querySelector('.side-tool.wechat-btn');
  if (wechatBtn) {
    wechatBtn.addEventListener('click', function (e) {
      if (window.innerWidth <= 640) {
        e.preventDefault();
        wechatBtn.classList.toggle('qr-open');
      }
    });
    document.addEventListener('click', function (e) {
      if (window.innerWidth <= 640 && !wechatBtn.contains(e.target)) {
        wechatBtn.classList.remove('qr-open');
      }
    });
  }

  // 移动端导航切换
  var navToggle = document.getElementById('navToggle');
  var nav = document.getElementById('nav');
  if (navToggle && nav) {
    navToggle.addEventListener('click', function () {
      var isOpen = nav.classList.toggle('open');
      navToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      navToggle.setAttribute('aria-label', isOpen ? '关闭菜单' : '打开菜单');
    });
    // 点击导航链接后收起菜单
    nav.querySelectorAll('a').forEach(function (link) {
      link.addEventListener('click', function () {
        nav.classList.remove('open');
        navToggle.setAttribute('aria-expanded', 'false');
      });
    });
  }

  // 页脚年份
  var year = document.getElementById('year');
  if (year) {
    year.textContent = new Date().getFullYear();
  }

  // 数字滚动：进入视口后从 0 递增到目标值
  var counters = document.querySelectorAll('[data-count]');
  function animateCounter(el) {
    var target = parseInt(el.getAttribute('data-count'), 10) || 0;
    var duration = 1200;
    var start = null;
    function step(timestamp) {
      if (!start) start = timestamp;
      var progress = Math.min((timestamp - start) / duration, 1);
      // 使用 easeOutCubic 让数字滚动更自然
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = Math.floor(eased * target);
      if (progress < 1) {
        window.requestAnimationFrame(step);
      } else {
        el.textContent = target;
      }
    }
    window.requestAnimationFrame(step);
  }

  if ('IntersectionObserver' in window && counters.length) {
    var counterObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          animateCounter(entry.target);
          counterObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.5 });
    counters.forEach(function (el) { counterObserver.observe(el); });
  } else {
    counters.forEach(function (el) {
      el.textContent = el.getAttribute('data-count');
    });
  }

  // 滚动渐显动画
  var revealSelectors = [
    '.section-head',
    '.hero-stats',
    '.hero-showcase',
    '.feature-card',
    '.timeline-step',
    '.deploy-card',
    '.bento-card',
    '.contact-item',
    '.price-card',
    '.case-card',
    '.cap-card',
    '.flow-strip',
    '.chat-demo',
    '.faq-item'
  ];
  var revealEls = [];
  revealSelectors.forEach(function (selector) {
    document.querySelectorAll(selector).forEach(function (el) {
      el.classList.add('reveal');
      revealEls.push(el);
    });
  });

  if ('IntersectionObserver' in window && revealEls.length) {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  } else {
    revealEls.forEach(function (el) { el.classList.add('visible'); });
  }

  // 手机端界面轮播
  var slides = document.querySelector('.phone-slides');
  var dots = document.querySelectorAll('.phone-dots .dot');
  if (slides && dots.length) {
    var phone = slides.closest('.phone');
    var current = 0;
    var total = dots.length;
    var timer = null;
    var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function goTo(index) {
      current = ((index % total) + total) % total;
      slides.style.transform = 'translateX(-' + current * 100 + '%)';
      dots.forEach(function (dot, i) {
        var active = i === current;
        dot.classList.toggle('active', active);
        dot.setAttribute('aria-selected', active ? 'true' : 'false');
      });
    }

    function start() {
      stop();
      if (!reduceMotion) {
        timer = setInterval(function () { goTo(current + 1); }, 4000);
      }
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
    }

    dots.forEach(function (dot, i) {
      dot.addEventListener('click', function () {
        goTo(i);
        start();
      });
    });

    if (phone) {
      phone.addEventListener('mouseenter', stop);
      phone.addEventListener('mouseleave', start);
      phone.addEventListener('touchstart', stop, { passive: true });
      phone.addEventListener('touchend', start, { passive: true });
    }

    goTo(0);
    start();
  }

  // 选点卡片轮播
  var spotCarousels = document.querySelectorAll('.spot-carousel');
  spotCarousels.forEach(function (carousel) {
    var spotSlides = carousel.querySelector('.spot-slides');
    var spotDots = carousel.querySelectorAll('.spot-dot');
    if (!spotSlides || !spotDots.length) { return; }

    var current = 0;
    var total = spotDots.length;
    var timer = null;
    var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function goTo(index) {
      current = ((index % total) + total) % total;
      spotSlides.style.transform = 'translateX(-' + current * 100 + '%)';
      spotDots.forEach(function (dot, i) {
        var active = i === current;
        dot.classList.toggle('active', active);
        dot.setAttribute('aria-selected', active ? 'true' : 'false');
      });
    }

    function start() {
      stop();
      if (!reduceMotion) {
        timer = setInterval(function () { goTo(current + 1); }, 4000);
      }
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
    }

    spotDots.forEach(function (dot, i) {
      dot.addEventListener('click', function () {
        goTo(i);
        start();
      });
    });

    carousel.addEventListener('mouseenter', stop);
    carousel.addEventListener('mouseleave', start);
    carousel.addEventListener('touchstart', stop, { passive: true });
    carousel.addEventListener('touchend', start, { passive: true });

    goTo(0);
    start();
  });

  // FAQ 手风琴：一次只展开一项
  var faqItems = document.querySelectorAll('.faq-item');
  if (faqItems.length) {
    function setFaq(item, open) {
      item.classList.toggle('open', open);
      var btn = item.querySelector('.faq-q');
      if (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    }

    faqItems.forEach(function (item) {
      var btn = item.querySelector('.faq-q');
      if (!btn) { return; }
      btn.addEventListener('click', function () {
        var willOpen = !item.classList.contains('open');
        faqItems.forEach(function (other) {
          if (other !== item) { setFaq(other, false); }
        });
        setFaq(item, willOpen);
      });
    });

    // 默认展开第一项，避免整块内容收起显得空洞
    setFaq(faqItems[0], true);
  }
});
