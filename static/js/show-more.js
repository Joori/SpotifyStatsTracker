(function () {
  function itemSelector() {
    return [
      ':scope > .insight-list > [data-show-more-item]',
      ':scope > .discovery-track-list > [data-show-more-item]',
      ':scope > .monthly-grid > [data-show-more-item]',
      ':scope > .track-list > [data-show-more-item]',
      ':scope > .peak-obsession-list > [data-show-more-item]',
      ':scope > [data-show-more-item]',
    ].join(', ');
  }

  function initShowMore(root) {
    (root || document).querySelectorAll('[data-show-more]').forEach((wrap) => {
      if (wrap.dataset.showMoreReady === '1') {
        return;
      }

      const initial = Math.max(1, Number(wrap.dataset.initial || 5));
      const step = Math.max(1, Number(wrap.dataset.step || initial));
      const noun = wrap.dataset.noun || 'more';
      const lessLabel = wrap.dataset.lessLabel || 'Show less';
      const items = Array.from(wrap.querySelectorAll(itemSelector()));
      const moreBtn = wrap.querySelector(':scope > .show-more [data-show-more-more]');
      const lessBtn = wrap.querySelector(':scope > .show-more [data-show-more-less]');
      const controls = wrap.querySelector(':scope > .show-more');

      if (!items.length) {
        return;
      }
      if (!moreBtn || items.length <= initial) {
        controls?.remove();
        items.forEach((item) => { item.hidden = false; });
        wrap.dataset.showMoreReady = '1';
        return;
      }

      let visible = initial;

      function render() {
        items.forEach((item, index) => {
          item.hidden = index >= visible;
        });
        const remaining = items.length - visible;
        moreBtn.hidden = remaining <= 0;
        lessBtn.hidden = visible <= initial;
        if (!moreBtn.hidden) {
          moreBtn.textContent = `Show ${Math.min(step, remaining)} ${noun}`;
        }
        if (lessBtn) {
          lessBtn.textContent = lessLabel;
        }
      }

      moreBtn.addEventListener('click', () => {
        visible = Math.min(items.length, visible + step);
        render();
      });
      lessBtn.addEventListener('click', () => {
        visible = Math.max(initial, visible - step);
        render();
      });

      wrap.dataset.showMoreReady = '1';
      render();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initShowMore(document));
  } else {
    initShowMore(document);
  }

  window.initShowMore = initShowMore;
})();
