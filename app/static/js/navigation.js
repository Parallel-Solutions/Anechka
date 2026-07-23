(function () {
    const button = document.getElementById('app-back-button');
    if (!button) return;

    button.addEventListener('click', () => {
        const fallback = button.dataset.fallback || '/';
        if (document.referrer) {
            try {
                const previous = new URL(document.referrer);
                const current = new URL(window.location.href);
                if (previous.origin === current.origin && previous.href !== current.href) {
                    window.history.back();
                    return;
                }
            } catch (_) {
                // Invalid referrer: use the deterministic application fallback.
            }
        }
        window.location.assign(fallback);
    });
})();