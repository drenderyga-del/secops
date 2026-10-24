document.addEventListener('DOMContentLoaded', () => {
    const workspace = document.querySelector('[data-integration-workspace]');
    const settingsPanel = document.querySelector('[data-integration-settings-panel]');
    const catalog = document.querySelector('.integration-catalog');
    const focusReturnKey = 'scanops.integrations.focus-return';

    const rememberFocusReturn = (controlId) => {
        if (!controlId) return;
        try {
            window.sessionStorage.setItem(focusReturnKey, controlId);
        } catch (_error) {
            // Hash-based restoration remains available when storage is blocked.
        }
    };

    const returnControlIdFromHash = () => {
        const decodedHash = decodeURIComponent(window.location.hash.slice(1));
        return decodedHash.startsWith('integration-return-') ? decodedHash : '';
    };

    const storedFocusReturn = () => {
        try {
            return window.sessionStorage.getItem(focusReturnKey) || '';
        } catch (_error) {
            return '';
        }
    };

    const clearStoredFocusReturn = () => {
        try {
            window.sessionStorage.removeItem(focusReturnKey);
        } catch (_error) {
            // No cleanup is required when storage is unavailable.
        }
    };

    const findIntegrationReturnControl = (controlId) => {
        const control = document.getElementById(controlId);
        return control?.matches('[data-integration-return-control]') ? control : null;
    };

    const restoreTriggerFocus = () => {
        if (document.querySelector('[data-integration-settings-panel]')) return;
        const controlId = returnControlIdFromHash() || storedFocusReturn();
        if (!controlId) return;

        const attemptFocus = (attemptsRemaining) => {
            const control = findIntegrationReturnControl(controlId);
            if (control) {
                control.focus({ preventScroll: true });
                if (document.activeElement === control) {
                    clearStoredFocusReturn();
                    return;
                }
            }

            if (attemptsRemaining > 0) {
                window.setTimeout(() => attemptFocus(attemptsRemaining - 1), 40);
            }
        };

        window.requestAnimationFrame(() => attemptFocus(3));
    };

    const copyEndpoint = async (button) => {
        const value = button.dataset.copyEndpoint || '';
        if (!value) return;

        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(value);
            } else {
                const fallback = document.createElement('textarea');
                fallback.value = value;
                fallback.setAttribute('readonly', '');
                fallback.style.position = 'fixed';
                fallback.style.opacity = '0';
                document.body.appendChild(fallback);
                fallback.select();
                document.execCommand('copy');
                fallback.remove();
            }
        } catch (_error) {
            return;
        }

        const label = button.querySelector('[data-copy-label]');
        if (!label) return;
        label.textContent = 'Copied';
        window.setTimeout(() => {
            label.textContent = 'Copy';
        }, 1600);
    };

    document.querySelectorAll('[data-copy-endpoint]').forEach((button) => {
        button.addEventListener('click', () => copyEndpoint(button));
    });

    const closeSettings = () => {
        if (!settingsPanel) return;
        const controlId = settingsPanel.dataset.integrationReturnControl || '';
        rememberFocusReturn(controlId);
        window.location.assign(`/integrations#${encodeURIComponent(controlId)}`);
    };

    const focusableElements = () => {
        if (!settingsPanel) return [];
        return Array.from(settingsPanel.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )).filter((element) => !element.hidden);
    };

    if (settingsPanel) {
        rememberFocusReturn(settingsPanel.dataset.integrationReturnControl || '');
        const syncPanelSemantics = () => {
            const isDrawer = window.getComputedStyle(settingsPanel).position === 'fixed';
            if (isDrawer) {
                settingsPanel.setAttribute('role', 'dialog');
                settingsPanel.setAttribute('aria-modal', 'true');
                if (catalog) catalog.inert = true;
            } else {
                settingsPanel.setAttribute('role', 'complementary');
                settingsPanel.removeAttribute('aria-modal');
                if (catalog) catalog.inert = false;
            }
        };

        syncPanelSemantics();
        window.addEventListener('resize', syncPanelSemantics);
        settingsPanel.focus({ preventScroll: true });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                closeSettings();
                return;
            }

            const isDrawer = window.getComputedStyle(settingsPanel).position === 'fixed';
            if (event.key !== 'Tab' || !isDrawer) return;
            const focusable = focusableElements();
            if (!focusable.length) {
                event.preventDefault();
                settingsPanel.focus();
                return;
            }

            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
    } else {
        restoreTriggerFocus();
    }

    document.querySelectorAll('[data-integration-close]').forEach((closeControl) => {
        closeControl.addEventListener('click', () => {
            rememberFocusReturn(
                closeControl.dataset.integrationReturnControl
                || settingsPanel?.dataset.integrationReturnControl
                || ''
            );
        });
    });
    document.querySelectorAll('[data-integration-opener]').forEach((opener) => {
        opener.addEventListener('click', () => {
            rememberFocusReturn(opener.dataset.integrationReturnControl || opener.id);
        });
    });
    window.addEventListener('pageshow', restoreTriggerFocus);
    window.addEventListener('hashchange', restoreTriggerFocus);

    document.querySelectorAll('[data-confirm-submit]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            if (!window.confirm(form.dataset.confirmSubmit || 'Continue?')) {
                event.preventDefault();
            }
        });
    });

    document.querySelectorAll('[data-submit-progress]').forEach((form) => {
        form.addEventListener('submit', (event) => {
            if (event.defaultPrevented) return;
            const button = event.submitter || (form.id
                ? document.querySelector(`[form="${CSS.escape(form.id)}"]`)
                : form.querySelector('button[type="submit"]'));
            if (!button) return;
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
            const label = button.querySelector('span');
            const progressLabel = form.dataset.submitProgress || 'Working…';
            if (label) {
                label.textContent = progressLabel;
            } else {
                button.textContent = progressLabel;
            }
        });
    });

    const list = document.querySelector('[data-gitlab-instances]');
    const template = document.querySelector('[data-gitlab-instance-template]');
    const addButton = document.querySelector('.gitlab-add-instance');

    if (!list || !template || !addButton) return;

    const bindCancelNew = (button) => {
        button.addEventListener('click', () => {
            button.closest('[data-gitlab-instance]')?.remove();
            addButton.disabled = false;
            addButton.focus();
            document.querySelector('[data-gitlab-empty]')?.removeAttribute('hidden');
        });
    };

    addButton.addEventListener('click', () => {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = template.innerHTML.trim();
        const editor = wrapper.firstElementChild;
        if (!editor) return;

        document.querySelector('[data-gitlab-empty]')?.setAttribute('hidden', '');
        list.appendChild(editor);
        addButton.disabled = true;
        bindCancelNew(editor.querySelector('.gitlab-cancel-new-instance'));
        editor.querySelector('input[type="text"]')?.focus();
    });

    workspace?.classList.add('is-enhanced');
});
