// Main JavaScript functionality for Security Dashboard

document.addEventListener('DOMContentLoaded', function() {
    // Initialize tooltips for job status indicators
    initializeTooltips();

    // Dashboard auto-refresh: every 5 minutes, only when the tab is visible
    // and the user is not interacting with a form. The old 30s full reload
    // reset scroll position and select states mid-use.
    if (window.location.pathname === '/') {
        initDashboardTopology();
        setInterval(refreshDashboardData, 300000);
    }

    initSidebarToggle();
    initSidebarHealth();
    initSignalSelects();
    initCommandPalette();

    // Add click handlers for interactive elements
    addInteractiveHandlers();

    // If a pipeline chart exists on the page, render it without external deps
    const pipelineCanvas = document.getElementById('pipelineChart');
    if (pipelineCanvas) {
        try {
            initCharts();
        } catch (err) {
            console.error('initCharts error', err);
        }
    }

    const durationCanvas = document.getElementById('durationChart');
    if (durationCanvas) {
        try {
            initDurationChart(durationCanvas);
        } catch (err) {
            console.error('initDurationChart error', err);
        }
    }

    document.querySelectorAll('[data-auto-submit="true"]').forEach(element => {
        element.addEventListener('change', () => {
            if (element.form) {
                if (typeof element.form.requestSubmit === 'function') {
                    element.form.requestSubmit();
                } else {
                    element.form.submit();
                }
            }
        });
    });

    setupAuditProductPicker();
    setupAuditScanStatus();
    setupFilterClearButtons();
    setupResponsiveTables();
    setupCopyButtons();
});

function initSignalSelects() {
    const selects = document.querySelectorAll('select:not([multiple])');
    let openSelect = null;

    selects.forEach((select, selectIndex) => {
        if (select.dataset.signalSelectReady === 'true') return;
        select.dataset.signalSelectReady = 'true';
        select.classList.add('signal-select-native');

        let host = select.parentElement;
        if (!host) return;
        if (!host.classList.contains('select') && !host.classList.contains('signal-select')) {
            const wrapper = document.createElement('span');
            wrapper.className = 'signal-select';
            host.insertBefore(wrapper, select);
            wrapper.appendChild(select);
            host = wrapper;
        }
        host.classList.add('signal-select');

        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'signal-select-trigger';
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        select.tabIndex = -1;
        const associatedLabel = select.labels && select.labels[0];
        if (associatedLabel) {
            trigger.setAttribute('aria-label', associatedLabel.textContent.trim());
            associatedLabel.addEventListener('click', (event) => {
                event.preventDefault();
                trigger.focus();
            });
        } else if (select.getAttribute('aria-label')) {
            trigger.setAttribute('aria-label', select.getAttribute('aria-label'));
        }

        const menu = document.createElement('div');
        menu.className = 'signal-select-menu';
        menu.id = `signal-select-menu-${selectIndex}`;
        menu.setAttribute('role', 'listbox');
        menu.setAttribute('aria-hidden', 'true');
        trigger.setAttribute('aria-controls', menu.id);
        document.body.appendChild(menu);

        const options = Array.from(select.options);

        function syncSelection() {
            const selected = options.find(option => option.value === select.value) || options[0];
            trigger.textContent = selected ? selected.textContent.trim() : '';
            menu.querySelectorAll('.signal-select-option').forEach((item) => {
                const isSelected = item.dataset.value === select.value;
                item.classList.toggle('is-selected', isSelected);
                item.setAttribute('aria-selected', isSelected ? 'true' : 'false');
            });
        }

        select.addEventListener('signal-select-sync', syncSelection);

        function positionMenu() {
            const rect = trigger.getBoundingClientRect();
            const gap = 6;
            const availableBelow = window.innerHeight - rect.bottom - gap;
            menu.style.left = `${Math.max(8, rect.left)}px`;
            menu.style.width = `${Math.max(rect.width, 180)}px`;
            menu.style.maxHeight = `${Math.max(120, Math.min(240, Math.max(availableBelow, rect.top - gap)))}px`;
            if (availableBelow < 180 && rect.top > availableBelow) {
                menu.style.top = 'auto';
                menu.style.bottom = `${window.innerHeight - rect.top + gap}px`;
            } else {
                menu.style.top = `${rect.bottom + gap}px`;
                menu.style.bottom = 'auto';
            }
        }

        function closeMenu({ restoreFocus = false } = {}) {
            if (!menu.classList.contains('is-open')) return;
            menu.classList.remove('is-open');
            menu.setAttribute('aria-hidden', 'true');
            trigger.setAttribute('aria-expanded', 'false');
            if (openSelect === closeMenu) openSelect = null;
            if (restoreFocus) trigger.focus();
        }

        function focusAdjacentControl(direction) {
            const selector = [
                'a[href]',
                'button:not([disabled])',
                'input:not([disabled]):not([type="hidden"])',
                'select:not([disabled])',
                'textarea:not([disabled])',
                '[tabindex]:not([tabindex="-1"])',
                '[contenteditable="true"]',
            ].join(',');
            const controls = Array.from(document.querySelectorAll(selector)).filter((element) => (
                element.tabIndex >= 0
                && !element.closest('.signal-select-menu')
                && !element.closest('[inert]')
                && element.getAttribute('aria-hidden') !== 'true'
                && element.getClientRects().length > 0
            ));
            const triggerIndex = controls.indexOf(trigger);
            const adjacent = triggerIndex >= 0 ? controls[triggerIndex + direction] : null;
            (adjacent || trigger).focus();
        }

        function openMenu() {
            if (select.disabled) return;
            if (openSelect && openSelect !== closeMenu) openSelect();
            openSelect = closeMenu;
            positionMenu();
            menu.classList.add('is-open');
            menu.setAttribute('aria-hidden', 'false');
            trigger.setAttribute('aria-expanded', 'true');
            const selected = menu.querySelector('.is-selected:not([disabled])');
            (selected || menu.querySelector('.signal-select-option:not([disabled])'))?.focus();
        }

        options.forEach((option) => {
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'signal-select-option';
            item.dataset.value = option.value;
            item.textContent = option.textContent.trim();
            item.setAttribute('role', 'option');
            item.disabled = option.disabled;
            item.addEventListener('click', () => {
                select.value = option.value;
                syncSelection();
                closeMenu({ restoreFocus: true });
                select.dispatchEvent(new Event('change', { bubbles: true }));
            });
            item.addEventListener('keydown', (event) => {
                const enabled = Array.from(menu.querySelectorAll('.signal-select-option:not([disabled])'));
                const index = enabled.indexOf(item);
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                    event.preventDefault();
                    const delta = event.key === 'ArrowDown' ? 1 : -1;
                    enabled[(index + delta + enabled.length) % enabled.length]?.focus();
                } else if (event.key === 'Escape') {
                    closeMenu({ restoreFocus: true });
                } else if (event.key === 'Tab') {
                    event.preventDefault();
                    closeMenu();
                    focusAdjacentControl(event.shiftKey ? -1 : 1);
                }
            });
            menu.appendChild(item);
        });

        trigger.addEventListener('click', () => {
            if (menu.classList.contains('is-open')) closeMenu();
            else openMenu();
        });
        trigger.addEventListener('keydown', (event) => {
            if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
                event.preventDefault();
                openMenu();
            }
        });
        select.addEventListener('change', syncSelection);
        host.appendChild(trigger);
        syncSelection();

        document.addEventListener('pointerdown', (event) => {
            if (!host.contains(event.target) && !menu.contains(event.target)) closeMenu();
        });
        window.addEventListener('resize', () => closeMenu());
        window.addEventListener('scroll', () => closeMenu(), true);
    });
}

// Initialize charts on the page (reads data from canvas[data-chart])
function initCharts() {
    const canvas = document.getElementById('pipelineChart');
    if (!canvas) return;

    // read JSON chart data from data-chart attribute
    let chartData = null;
    try {
        const raw = canvas.getAttribute('data-chart');
        chartData = raw ? JSON.parse(raw) : null;
    } catch (e) {
        console.error('Failed to parse chart data', e);
        return;
    }

    if (!chartData) return;

    renderPipelineChart(canvas, chartData);
}

function renderPipelineChart(canvas, chartData) {
    const labels = Array.isArray(chartData.labels) ? chartData.labels : [];
    const displayLabels = Array.isArray(chartData.display_labels) ? chartData.display_labels : labels;
    const failed = Array.isArray(chartData.failed) ? chartData.failed.map(value => Number(value) || 0) : [];
    const successful = Array.isArray(chartData.success) ? chartData.success.map(value => Number(value) || 0) : [];

    if (!labels.length) return;

    const totalAt = index => (successful[index] || 0) + (failed[index] || 0);
    const totalObserved = labels.reduce((sum, _, index) => sum + totalAt(index), 0);
    const tooltip = document.createElement('div');
    tooltip.className = 'pipeline-chart-tooltip';
    tooltip.hidden = true;
    document.body.appendChild(tooltip);

    let hoverIndex = null;
    let layoutState = null;

    const niceCeiling = value => {
        if (value <= 0) return 1;
        const magnitude = 10 ** Math.floor(Math.log10(value));
        const normalized = value / magnitude;
        const rounded = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
        return rounded * magnitude;
    };

    const drawTopRoundedRect = (ctx, x, y, width, height, radius) => {
        if (height <= 0) return;
        const r = Math.min(radius, width / 2, height);
        ctx.beginPath();
        ctx.moveTo(x, y + height);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.lineTo(x + width - r, y);
        ctx.quadraticCurveTo(x + width, y, x + width, y + r);
        ctx.lineTo(x + width, y + height);
        ctx.closePath();
        ctx.fill();
    };

    const render = () => {
        const width = canvas.clientWidth || 640;
        const height = canvas.clientHeight || 320;
        const deviceRatio = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.round(width * deviceRatio);
        canvas.height = Math.round(height * deviceRatio);

        const ctx = canvas.getContext('2d');
        ctx.setTransform(deviceRatio, 0, 0, deviceRatio, 0, 0);
        ctx.clearRect(0, 0, width, height);

        const margin = {
            top: 14,
            right: width < 520 ? 8 : 16,
            bottom: 38,
            left: width < 520 ? 34 : 44,
        };
        const plotWidth = Math.max(10, width - margin.left - margin.right);
        const plotHeight = Math.max(10, height - margin.top - margin.bottom);
        const slotWidth = plotWidth / labels.length;
        const barWidth = Math.max(4, Math.min(22, slotWidth * 0.58));
        const maxValue = niceCeiling(Math.max(1, ...labels.map((_, index) => totalAt(index))));
        const baseline = margin.top + plotHeight;
        const css = getComputedStyle(document.documentElement);
        const textMuted = css.getPropertyValue('--text-muted').trim() || '#9690a4';
        const textSecondary = css.getPropertyValue('--text-secondary').trim() || '#c9c1d5';
        const textPrimary = css.getPropertyValue('--text-primary').trim() || '#f5f2f8';
        const chartPrimary = css.getPropertyValue('--chart-primary').trim() || '#8f64e8';
        const chartPrimaryFade = css.getPropertyValue('--chart-primary-fade').trim() || 'rgba(143, 100, 232, 0.14)';
        const chartCurrent = css.getPropertyValue('--chart-current').trim() || '#a7f44d';
        const chartCurrentFade = css.getPropertyValue('--chart-current-fade').trim() || 'rgba(167, 244, 77, 0.14)';
        const chartHover = css.getPropertyValue('--chart-hover').trim() || 'rgba(143, 100, 232, 0.055)';
        const failedColor = css.getPropertyValue('--error').trim() || '#f05f67';

        const xFor = index => margin.left + slotWidth * index + slotWidth / 2;
        const yFor = value => baseline - (value / maxValue) * plotHeight;

        ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'right';
        for (let tick = 0; tick <= 4; tick += 1) {
            const value = (maxValue / 4) * tick;
            const y = yFor(value);
            ctx.strokeStyle = tick === 0 ? 'rgba(196, 181, 211, 0.22)' : 'rgba(196, 181, 211, 0.09)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(margin.left, y + 0.5);
            ctx.lineTo(margin.left + plotWidth, y + 0.5);
            ctx.stroke();
            ctx.fillStyle = textMuted;
            ctx.fillText(Math.round(value).toLocaleString(), margin.left - 9, y);
        }

        if (hoverIndex !== null) {
            const center = xFor(hoverIndex);
            ctx.fillStyle = chartHover;
            ctx.fillRect(center - slotWidth / 2, margin.top, slotWidth, plotHeight);
        }

        labels.forEach((_, index) => {
            const successValue = successful[index] || 0;
            const failedValue = failed[index] || 0;
            const totalValue = successValue + failedValue;
            const x = xFor(index) - barWidth / 2;
            const totalY = yFor(totalValue);
            const failedY = yFor(failedValue);
            const successHeight = failedY - totalY;
            const failedHeight = baseline - failedY;
            const isCurrent = index === labels.length - 1;

            if (successHeight > 0) {
                const successGradient = ctx.createLinearGradient(0, totalY, 0, failedY);
                successGradient.addColorStop(0, isCurrent ? chartCurrent : chartPrimary);
                successGradient.addColorStop(1, isCurrent ? chartCurrentFade : chartPrimaryFade);
                ctx.fillStyle = successGradient;
                drawTopRoundedRect(ctx, x, totalY, barWidth, successHeight, 4);
            }

            if (failedHeight > 0) {
                ctx.fillStyle = failedColor;
                if (successHeight > 0) {
                    ctx.fillRect(x, failedY, barWidth, failedHeight);
                } else {
                    drawTopRoundedRect(ctx, x, failedY, barWidth, failedHeight, 4);
                }
            }

            if (hoverIndex === index && totalValue > 0) {
                ctx.save();
                ctx.strokeStyle = textPrimary;
                ctx.globalAlpha = 0.72;
                ctx.lineWidth = 1;
                ctx.beginPath();
                if (typeof ctx.roundRect === 'function') {
                    ctx.roundRect(x - 1.5, totalY - 1.5, barWidth + 3, baseline - totalY + 3, 5);
                } else {
                    ctx.rect(x - 1.5, totalY - 1.5, barWidth + 3, baseline - totalY + 3);
                }
                ctx.stroke();
                ctx.restore();
            }
        });

        const minLabelWidth = width < 520 ? 68 : 58;
        const labelStep = Math.max(1, Math.ceil(minLabelWidth / slotWidth));
        ctx.fillStyle = textMuted;
        ctx.font = '11px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        labels.forEach((label, index) => {
            if (index % labelStep !== 0 && index !== labels.length - 1) return;
            ctx.fillText(String(displayLabels[index] || label), xFor(index), baseline + 12);
        });

        if (!totalObserved) {
            ctx.fillStyle = textSecondary;
            ctx.font = '13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('No executions in this window', margin.left + plotWidth / 2, margin.top + plotHeight / 2);
        }

        layoutState = { margin, plotWidth, plotHeight, slotWidth, xFor, yFor, baseline };
    };

    const showTooltip = (index, clientX, clientY) => {
        const title = document.createElement('strong');
        title.textContent = String(displayLabels[index] || labels[index] || '');
        const totalRow = document.createElement('span');
        totalRow.innerHTML = '<i class="tooltip-total"></i><em>Total</em><b></b>';
        totalRow.querySelector('b').textContent = String(totalAt(index));
        const successRow = document.createElement('span');
        successRow.innerHTML = '<i class="tooltip-success"></i><em>Successful</em><b></b>';
        successRow.querySelector('b').textContent = String(successful[index] || 0);
        const failedRow = document.createElement('span');
        failedRow.innerHTML = '<i class="tooltip-failed"></i><em>Failed</em><b></b>';
        failedRow.querySelector('b').textContent = String(failed[index] || 0);
        tooltip.replaceChildren(title, totalRow, successRow, failedRow);
        tooltip.hidden = false;
        tooltip.style.left = `${Math.min(clientX + 14, window.innerWidth - tooltip.offsetWidth - 12)}px`;
        tooltip.style.top = `${Math.max(12, clientY - tooltip.offsetHeight - 14)}px`;
    };

    const setHoverIndex = index => {
        hoverIndex = Math.max(0, Math.min(labels.length - 1, index));
        render();
    };

    canvas.addEventListener('pointermove', event => {
        if (!layoutState) return;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const { margin, plotWidth, plotHeight, slotWidth } = layoutState;
        if (x < margin.left || x > margin.left + plotWidth || y < margin.top || y > margin.top + plotHeight) {
            tooltip.hidden = true;
            return;
        }
        const index = Math.min(labels.length - 1, Math.max(0, Math.floor((x - margin.left) / slotWidth)));
        if (hoverIndex !== index) setHoverIndex(index);
        showTooltip(index, event.clientX, event.clientY);
    });

    canvas.addEventListener('pointerleave', () => {
        tooltip.hidden = true;
        if (hoverIndex !== null) {
            hoverIndex = null;
            render();
        }
    });

    canvas.addEventListener('focus', () => {
        if (hoverIndex === null) setHoverIndex(labels.length - 1);
    });

    canvas.addEventListener('blur', () => {
        hoverIndex = null;
        tooltip.hidden = true;
        render();
    });

    canvas.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Escape'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Escape') {
            hoverIndex = null;
            tooltip.hidden = true;
            render();
            return;
        }
        const nextIndex = hoverIndex === null
            ? labels.length - 1
            : hoverIndex + (event.key === 'ArrowRight' ? 1 : -1);
        setHoverIndex(nextIndex);
        const rect = canvas.getBoundingClientRect();
        showTooltip(
            hoverIndex,
            rect.left + layoutState.xFor(hoverIndex),
            rect.top + layoutState.yFor(totalAt(hoverIndex)),
        );
    });

    if (typeof ResizeObserver !== 'undefined') {
        const resizeObserver = new ResizeObserver(render);
        resizeObserver.observe(canvas);
    } else {
        window.addEventListener('resize', render);
    }
    render();
}

function initDurationChart(canvas) {
    let chartData = null;
    try {
        const raw = canvas.getAttribute('data-chart');
        chartData = raw ? JSON.parse(raw) : null;
    } catch (e) {
        console.error('Failed to parse duration chart data', e);
        return;
    }
    if (!chartData) return;
    renderDurationChart(canvas, chartData);
}

function renderDurationChart(canvas, chartData) {
    const labels = Array.isArray(chartData.labels) ? chartData.labels : [];
    const displayLabels = Array.isArray(chartData.display_labels) ? chartData.display_labels : labels;
    const mode = chartData.mode || 'daily';
    const pipelineAvg = Array.isArray(chartData.pipeline_avg) ? chartData.pipeline_avg.map(value => value === null ? null : Number(value)) : [];
    const jobAvg = Array.isArray(chartData.job_avg) ? chartData.job_avg.map(value => value === null ? null : Number(value)) : [];
    const pipelineCount = Array.isArray(chartData.pipeline_count) ? chartData.pipeline_count.map(Number) : [];
    const jobCount = Array.isArray(chartData.job_count) ? chartData.job_count.map(Number) : [];

    if (!labels.length) return;

    const deviceRatio = window.devicePixelRatio || 1;
    let hoverIndex = null;

    const visibleValues = [...pipelineAvg, ...jobAvg].filter(value => value !== null && Number.isFinite(value));
    const maxValue = Math.max(1, ...visibleValues);

    const render = () => {
        const width = canvas.clientWidth || 600;
        const height = canvas.clientHeight || 380;
        const narrowViewport = typeof window.matchMedia === 'function'
            && window.matchMedia('(max-width: 820px)').matches;
        const compactCanvas = narrowViewport && width <= 520;
        canvas.width = width * deviceRatio;
        canvas.height = height * deviceRatio;
        const ctx = canvas.getContext('2d');
        ctx.setTransform(deviceRatio, 0, 0, deviceRatio, 0, 0);
        ctx.clearRect(0, 0, width, height);

        const margin = compactCanvas
            ? { top: 36, right: 18, bottom: 96, left: 54 }
            : { top: 40, right: 56, bottom: 110, left: 64 };
        const plotWidth = Math.max(10, width - margin.left - margin.right);
        const plotHeight = Math.max(10, height - margin.top - margin.bottom);

        ctx.fillStyle = 'rgba(20, 27, 23, 0.65)';
        ctx.fillRect(margin.left, margin.top, plotWidth, plotHeight);

        const gridLines = 4;
        ctx.lineWidth = 1;
        ctx.strokeStyle = 'rgba(148, 163, 184, 0.12)';
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-muted') || '#94a3b8';
        ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        for (let i = 0; i <= gridLines; i++) {
            const y = margin.top + (plotHeight / gridLines) * i;
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(margin.left + plotWidth, y);
            ctx.stroke();
            const value = Math.round(maxValue * (1 - i / gridLines));
            ctx.fillText(`${value} min`, margin.left - 54, y + 4);
        }

        const step = labels.length > 1 ? plotWidth / (labels.length - 1) : 0;
        const getX = index => margin.left + (labels.length === 1 ? plotWidth / 2 : step * index);
        const getY = value => margin.top + plotHeight - (value / maxValue) * plotHeight;

        const drawSeries = (values, color, fillColor) => {
            let started = false;
            ctx.strokeStyle = color;
            ctx.lineWidth = 2.4;
            ctx.lineJoin = 'round';
            ctx.lineCap = 'round';
            ctx.beginPath();
            values.forEach((value, index) => {
                if (value === null || !Number.isFinite(value)) {
                    started = false;
                    return;
                }
                const x = getX(index);
                const y = getY(value);
                if (!started) {
                    ctx.moveTo(x, y);
                    started = true;
                } else {
                    ctx.lineTo(x, y);
                }
            });
            ctx.stroke();

            ctx.fillStyle = color;
            values.forEach((value, index) => {
                if (value === null || !Number.isFinite(value)) return;
                const x = getX(index);
                const y = getY(value);
                ctx.beginPath();
                ctx.arc(x, y, 4.5, 0, Math.PI * 2);
                ctx.fill();
            });
        };

        drawSeries(pipelineAvg, '#86efac');
        drawSeries(jobAvg, '#facc15');

        if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < labels.length) {
            const hx = getX(hoverIndex);
            ctx.save();
            ctx.strokeStyle = 'rgba(52, 211, 155, 0.55)';
            ctx.setLineDash([4, 4]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(hx, margin.top);
            ctx.lineTo(hx, margin.top + plotHeight);
            ctx.stroke();
            ctx.restore();

            [
                [pipelineAvg[hoverIndex], '#86efac'],
                [jobAvg[hoverIndex], '#facc15']
            ].forEach(([value, color]) => {
                if (value === null || !Number.isFinite(value)) return;
                const y = getY(value);
                ctx.beginPath();
                ctx.fillStyle = '#ffffff';
                ctx.arc(hx, y, 7, 0, Math.PI * 2);
                ctx.fill();
                ctx.beginPath();
                ctx.fillStyle = color;
                ctx.arc(hx, y, 4.8, 0, Math.PI * 2);
                ctx.fill();
            });
        }

        ctx.strokeStyle = 'rgba(148, 163, 184, 0.3)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(margin.left, margin.top + plotHeight);
        ctx.lineTo(margin.left + plotWidth, margin.top + plotHeight);
        ctx.stroke();

        ctx.save();
        ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text-primary') || '#e2e8f0';
        ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        let compactTickIndexes = null;
        let labelStep = 1;
        if (compactCanvas) {
            const tickCount = Math.min(labels.length, Math.max(2, Math.floor(plotWidth / 60)));
            compactTickIndexes = new Set();
            for (let tick = 0; tick < tickCount; tick++) {
                const index = Math.round(
                    tick * (labels.length - 1) / Math.max(1, tickCount - 1),
                );
                compactTickIndexes.add(index);
            }
        } else if (mode === 'daily' && labels.length >= 25) {
            labelStep = 5;
        } else if (labels.length > 12) {
            labelStep = Math.ceil(labels.length / 12);
        }
        let renderedTickCount = 0;
        labels.forEach((_, index) => {
            if (compactTickIndexes && !compactTickIndexes.has(index)) return;
            if (!compactTickIndexes && labelStep > 1 && index % labelStep !== 0 && index !== labels.length - 1) return;
            const text = String(displayLabels[index] || labels[index] || '');
            const x = getX(index);
            const textWidth = ctx.measureText(text).width;
            ctx.fillText(text, x - textWidth / 2, margin.top + plotHeight + 22);
            renderedTickCount += 1;
        });
        ctx.restore();

        canvas.dataset.durationTickCount = String(renderedTickCount);
        canvas.dataset.durationLegendLayout = compactCanvas ? 'stacked' : 'inline';

        const legendY = margin.top + plotHeight + 50;
        ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        ctx.fillStyle = '#86efac';
        ctx.fillText('Avg pipeline duration', margin.left, legendY);
        ctx.fillStyle = '#facc15';
        ctx.fillText(
            'Avg job duration',
            compactCanvas ? margin.left : margin.left + 180,
            compactCanvas ? legendY + 21 : legendY,
        );

        const ensureTooltip = () => {
            let tooltip = document.querySelector('.pipeline-point-tooltip');
            if (!tooltip) {
                tooltip = document.createElement('div');
                tooltip.className = 'pipeline-point-tooltip';
                tooltip.style.cssText = `
                    position: fixed;
                    z-index: 1200;
                    pointer-events: none;
                    background: rgba(20, 27, 23, 0.96);
                    color: #f8fbff;
                    border: 1px solid rgba(52, 211, 155, 0.42);
                    border-radius: 10px;
                    padding: 0.45rem 0.6rem;
                    font-size: 0.78rem;
                    line-height: 1.35;
                    box-shadow: 0 10px 24px rgba(8, 15, 34, 0.55);
                    display: none;
                    white-space: nowrap;
                `;
                document.body.appendChild(tooltip);
            }
            return tooltip;
        };

        canvas.onmousemove = (event) => {
            const rect = canvas.getBoundingClientRect();
            const mx = event.clientX - rect.left;
            const my = event.clientY - rect.top;
            const inPlotX = mx >= margin.left && mx <= margin.left + plotWidth;
            const inPlotY = my >= margin.top && my <= margin.top + plotHeight;
            if (!inPlotX || !inPlotY) {
                const tip = document.querySelector('.pipeline-point-tooltip');
                if (tip) tip.style.display = 'none';
                return;
            }

            let nearestIndex = 0;
            let nearestDistance = Number.POSITIVE_INFINITY;
            for (let i = 0; i < labels.length; i++) {
                const distance = Math.abs(getX(i) - mx);
                if (distance < nearestDistance) {
                    nearestDistance = distance;
                    nearestIndex = i;
                }
            }
            if (nearestDistance > 20) {
                const tip = document.querySelector('.pipeline-point-tooltip');
                if (tip) tip.style.display = 'none';
                if (hoverIndex !== null) {
                    hoverIndex = null;
                    render();
                }
                return;
            }

            if (hoverIndex !== nearestIndex) {
                hoverIndex = nearestIndex;
                render();
            }

            const pipelineValue = pipelineAvg[nearestIndex];
            const jobValue = jobAvg[nearestIndex];
            const pointLabel = String(displayLabels[nearestIndex] || labels[nearestIndex] || '');
            const fmt = value => value === null || !Number.isFinite(value) ? '—' : `${Math.round(value)} min`;
            const tooltip = ensureTooltip();
            tooltip.innerHTML =
                `<strong>${pointLabel}</strong><br>` +
                `Pipeline avg: ${fmt(pipelineValue)} (${pipelineCount[nearestIndex] || 0} runs)<br>` +
                `Job avg: ${fmt(jobValue)} (${jobCount[nearestIndex] || 0} jobs)`;
            tooltip.style.left = `${event.clientX + 14}px`;
            tooltip.style.top = `${event.clientY - 12}px`;
            tooltip.style.display = 'block';
        };

        canvas.onmouseleave = () => {
            const tip = document.querySelector('.pipeline-point-tooltip');
            if (tip) tip.style.display = 'none';
            if (hoverIndex !== null) {
                hoverIndex = null;
                render();
            }
        };
    };

    if (typeof ResizeObserver !== 'undefined') {
        const resizeObserver = new ResizeObserver(() => render());
        resizeObserver.observe(canvas);
    } else {
        window.addEventListener('resize', render);
    }
    render();
}

function initializeTooltips() {
    const jobStatuses = document.querySelectorAll('.job-status');
    jobStatuses.forEach(status => {
        status.addEventListener('mouseenter', function() {
            const tooltip = document.createElement('div');
            tooltip.className = 'tooltip';
            tooltip.textContent = this.getAttribute('title');
            tooltip.style.cssText = `
                position: absolute;
                background: var(--bg-tertiary);
                color: var(--text-primary);
                padding: 0.5rem;
                border-radius: 4px;
                font-size: 0.75rem;
                z-index: 1000;
                pointer-events: none;
                border: 1px solid var(--border);
            `;
            document.body.appendChild(tooltip);

            const rect = this.getBoundingClientRect();
            tooltip.style.left = rect.left + 'px';
            tooltip.style.top = (rect.top - tooltip.offsetHeight - 5) + 'px';
        });

        status.addEventListener('mouseleave', function() {
            const tooltip = document.querySelector('.tooltip');
            if (tooltip) {
                tooltip.remove();
            }
        });
    });
}

function refreshDashboardData() {
    if (document.hidden) return;
    const active = document.activeElement;
    if (active && (active.tagName === 'SELECT' || active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
        return;
    }
    window.location.reload();
}

function initDashboardTopology() {
    const panel = document.querySelector('[data-topology-url]');
    if (!panel) return;

    const states = document.querySelectorAll('[data-topology-state]');
    const overviewSignal = document.querySelector('[data-overview-signal]');
    let requestInFlight = false;

    function setMetric(name, value) {
        panel.querySelectorAll(`[data-topology-metric="${name}"]`).forEach((node) => {
            node.textContent = String(value);
        });
    }

    function relativeTime(value) {
        if (!value) return 'not recorded';
        const parsed = Date.parse(value);
        if (!Number.isFinite(parsed)) return 'unknown';
        const seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
        if (seconds < 60) return 'just now';
        if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
        return `${Math.round(seconds / 86400)}d ago`;
    }

    function setLastExecution(value) {
        panel.querySelectorAll('[data-topology-last-execution]').forEach((node) => {
            node.dateTime = value || '';
            node.textContent = relativeTime(value);
            node.title = value ? `Last execution at ${new Date(value).toLocaleString()}` : 'No execution has been recorded';
        });
    }

    function syncRiverStageStates(data) {
        const metric = name => Number(data[name]) || 0;
        const riverStates = {
            source: metric('gitlab_triggers') > 0 ? 'complete' : 'idle',
            queue: metric('active_pipelines') > 0
                ? 'active'
                : metric('execution_total') > 0 ? 'complete' : 'idle',
            controls: metric('execution_failed') > 0
                ? 'problem'
                : metric('active_pipelines') > 0
                    ? 'active'
                    : metric('execution_total') > 0 ? 'complete' : 'idle',
            evidence: metric('normalized_outputs') > 0
                ? 'complete'
                : metric('execution_completed') > 0 ? 'warning' : 'idle',
            delivery: metric('total_products') > 0
                && metric('asoc_mapped_products') >= metric('total_products')
                ? 'complete'
                : metric('total_products') > 0 ? 'warning' : 'idle',
        };
        const telemetryState = data.telemetry_state || 'empty';
        const stateLabels = {
            active: 'active',
            complete: 'complete',
            warning: 'warning',
            problem: 'attention',
            idle: 'waiting',
            stale: 'last known',
        };

        Object.entries(riverStates).forEach(([stage, state]) => {
            const node = panel.querySelector(`[data-river-stage="${stage}"]`);
            if (!node) return;
            const effectiveState = telemetryState === 'stale'
                ? 'stale'
                : telemetryState === 'empty' ? 'idle' : state;
            node.classList.remove(
                'river-stage-idle',
                'river-stage-active',
                'river-stage-complete',
                'river-stage-warning',
                'river-stage-problem',
                'river-stage-stale',
            );
            node.classList.add(`river-stage-${effectiveState}`);
            node.dataset.riverState = effectiveState;
            const label = node.querySelector('[data-river-state-label]');
            if (label) label.textContent = stateLabels[effectiveState];
        });
    }

    function syncOverviewSignal(data) {
        if (!overviewSignal) return;
        const title = overviewSignal.querySelector('[data-overview-signal-title]');
        const context = overviewSignal.querySelector('[data-overview-signal-context]');
        const action = overviewSignal.querySelector('[data-overview-signal-action]');
        const failedJobs = Number(data.failed_outputs) || 0;
        const configFindings = Number(data.config_drift) || 0;
        const iacFindings = Number(overviewSignal.dataset.iacAttention) || 0;
        const coverageGaps = Number(overviewSignal.dataset.coverageGaps) || 0;
        const issueParts = [
            failedJobs ? `${failedJobs} failed jobs` : '',
            configFindings ? `${configFindings} configuration findings` : '',
            iacFindings ? `${iacFindings} critical/high IaC findings` : '',
            coverageGaps ? `${coverageGaps} coverage gaps` : '',
        ].filter(Boolean);
        const timeframe = new URL(panel.dataset.topologyUrl, window.location.origin).searchParams.get('timeframe') || '7d';
        let tone = 'healthy';

        if (data.telemetry_state === 'stale') {
            tone = 'stale';
            if (title) title.textContent = `Latest execution signal arrived ${relativeTime(data.last_execution_at)}.`;
            if (context) context.textContent = ['Showing last known state', ...issueParts].join(' · ');
        } else if (data.telemetry_state === 'empty') {
            tone = 'empty';
            if (title) title.textContent = 'No execution telemetry has been recorded yet.';
            if (context) context.textContent = 'Connect a source or run a pipeline to establish the first operational signal.';
        } else if (failedJobs) {
            tone = 'problem';
            if (title) title.textContent = `${failedJobs} failed jobs need operator context.`;
            if (context) context.textContent = issueParts.join(' · ');
        } else if (configFindings || iacFindings || coverageGaps) {
            tone = 'warning';
            if (title) title.textContent = 'Execution delivery is current, with posture findings to review.';
            if (context) context.textContent = issueParts.join(' · ');
        } else {
            if (title) title.textContent = 'Execution telemetry is current.';
            if (context) context.textContent = 'No critical execution or posture backlog is visible in this window.';
        }

        overviewSignal.classList.remove(
            'overview-action-strip-healthy',
            'overview-action-strip-warning',
            'overview-action-strip-problem',
            'overview-action-strip-stale',
            'overview-action-strip-empty',
        );
        overviewSignal.classList.add(`overview-action-strip-${tone}`);

        if (action) {
            if (data.telemetry_state === 'empty') {
                action.href = '/integrations';
                action.textContent = 'Open integrations';
            } else if (failedJobs) {
                action.href = `/logs?status=failed&timeframe=${encodeURIComponent(timeframe)}`;
                action.textContent = data.telemetry_state === 'live' ? 'Resolve failed jobs' : 'Inspect failed jobs';
            } else {
                action.href = `/logs?timeframe=${encodeURIComponent(timeframe)}`;
                action.textContent = 'Open executions';
            }
        }
    }

    function setTopologyState(value, data = {}) {
        states.forEach((state) => {
            const container = state.closest('.overview-live-state');
            if (container) {
                container.classList.toggle('overview-live-state-live', value === 'live');
                container.classList.toggle('overview-live-state-stale', value === 'stale');
                container.classList.toggle('overview-live-state-empty', value === 'empty');
                container.setAttribute('aria-label', `Telemetry state: ${value}`);
            }
            state.textContent = value === 'live'
                ? 'live'
                : value === 'stale'
                    ? 'stale signal'
                    : 'no signal';
            state.title = value === 'live'
                ? `Dashboard refreshed ${new Date(data.updated_at || Date.now()).toLocaleTimeString()}`
                : value === 'stale'
                    ? `The latest execution is older than ${Math.round((data.telemetry_stale_after_seconds || 0) / 3600)}h`
                    : 'The dashboard is reachable, but no execution telemetry has been recorded';
        });
    }

    async function pollTopology() {
        if (document.hidden || requestInFlight) return;
        requestInFlight = true;
        try {
            const response = await fetch(panel.dataset.topologyUrl, { cache: 'no-store' });
            if (!response.ok) throw new Error(`Topology request failed: ${response.status}`);
            const data = await response.json();
            Object.entries(data).forEach(([name, value]) => {
                if (!['updated_at', 'last_execution_at', 'telemetry_state', 'telemetry_age_seconds', 'telemetry_stale_after_seconds'].includes(name)) setMetric(name, value);
            });
            setLastExecution(data.last_execution_at);
            syncRiverStageStates(data);
            syncOverviewSignal(data);
            panel.classList.toggle('is-stale', data.telemetry_state === 'stale');
            panel.classList.toggle('is-empty', data.telemetry_state === 'empty');
            setTopologyState(data.telemetry_state, data);
        } catch (_error) {
            panel.classList.add('is-stale');
            panel.classList.remove('is-empty');
            states.forEach((state) => {
                state.textContent = 'stale';
                state.title = 'Live metrics are temporarily unavailable';
                const container = state.closest('.overview-live-state');
                if (container) {
                    container.classList.remove('overview-live-state-live', 'overview-live-state-empty');
                    container.classList.add('overview-live-state-stale');
                    container.setAttribute('aria-label', 'Telemetry state: stale');
                }
            });
            if (overviewSignal) {
                overviewSignal.classList.remove(
                    'overview-action-strip-healthy',
                    'overview-action-strip-warning',
                    'overview-action-strip-problem',
                    'overview-action-strip-empty',
                );
                overviewSignal.classList.add('overview-action-strip-stale');
                const title = overviewSignal.querySelector('[data-overview-signal-title]');
                const context = overviewSignal.querySelector('[data-overview-signal-context]');
                if (title) title.textContent = 'Live execution telemetry is temporarily unavailable.';
                if (context) context.textContent = 'Showing the last successfully loaded state.';
            }
        } finally {
            requestInFlight = false;
        }
    }

    pollTopology();
    setInterval(pollTopology, 15000);
}

function initSidebarToggle() {
    const button = document.getElementById('sidebar-toggle');
    const layout = document.body;
    if (!button || !layout) return;

    const storageKey = 'secpipe.sidebar.collapsed';

    function apply(collapsed) {
        layout.classList.toggle('sidebar-collapsed', collapsed);
        button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        button.setAttribute('title', collapsed ? 'Expand navigation' : 'Collapse navigation');
    }

    const collapsed = window.localStorage.getItem(storageKey) === 'true';
    apply(collapsed);

    button.addEventListener('click', (event) => {
        const next = !layout.classList.contains('sidebar-collapsed');
        window.localStorage.setItem(storageKey, String(next));
        apply(next);
        if (event.detail > 0) {
            button.blur();
        }
    });
}

function initCommandPalette() {
    const dialog = document.getElementById('command-palette');
    const trigger = document.getElementById('command-palette-trigger');
    const search = document.getElementById('command-palette-search');
    if (!dialog || !trigger || !search) return;

    const links = Array.from(dialog.querySelectorAll('a[data-command-label]'));
    const sections = Array.from(dialog.querySelectorAll('.command-palette-section'));
    const empty = dialog.querySelector('.command-palette-empty');

    function visibleLinks() {
        return links.filter(link => !link.hidden);
    }

    function syncSections() {
        sections.forEach((section) => {
            let sibling = section.nextElementSibling;
            let hasVisibleLink = false;
            while (sibling && !sibling.classList.contains('command-palette-section')) {
                if (sibling.matches('a[data-command-label]') && !sibling.hidden) {
                    hasVisibleLink = true;
                    break;
                }
                sibling = sibling.nextElementSibling;
            }
            section.hidden = !hasVisibleLink;
        });
    }

    function filterLinks() {
        const query = search.value.trim().toLowerCase();
        links.forEach((link) => {
            link.hidden = Boolean(query) && !link.dataset.commandLabel.toLowerCase().includes(query);
            link.classList.remove('is-active');
        });
        syncSections();
        if (empty) empty.hidden = visibleLinks().length !== 0;
    }

    function openPalette() {
        if (dialog.open) return;
        if (typeof dialog.showModal === 'function') dialog.showModal();
        else dialog.setAttribute('open', '');
        search.value = '';
        filterLinks();
        window.requestAnimationFrame(() => search.focus());
    }

    function closePalette() {
        if (!dialog.open) return;
        if (typeof dialog.close === 'function') dialog.close();
        else dialog.removeAttribute('open');
    }

    function moveFocus(direction) {
        const visible = visibleLinks();
        if (!visible.length) return;
        const active = document.activeElement;
        const current = visible.indexOf(active);
        const next = current < 0
            ? (direction > 0 ? 0 : visible.length - 1)
            : (current + direction + visible.length) % visible.length;
        visible.forEach(link => link.classList.remove('is-active'));
        visible[next].classList.add('is-active');
        visible[next].focus();
    }

    trigger.addEventListener('click', openPalette);
    search.addEventListener('input', filterLinks);
    search.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            moveFocus(event.key === 'ArrowDown' ? 1 : -1);
        } else if (event.key === 'Enter') {
            const first = visibleLinks()[0];
            if (first) {
                event.preventDefault();
                first.click();
            }
        }
    });

    links.forEach((link) => {
        link.addEventListener('keydown', (event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                moveFocus(event.key === 'ArrowDown' ? 1 : -1);
            }
        });
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && dialog.open) {
            event.preventDefault();
            closePalette();
            return;
        }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault();
            if (dialog.open) closePalette();
            else openPalette();
        }
    });

    dialog.addEventListener('click', (event) => {
        if (event.target === dialog) closePalette();
    });
    dialog.addEventListener('cancel', (event) => {
        event.preventDefault();
        closePalette();
    });
    dialog.addEventListener('close', () => {
        search.value = '';
        filterLinks();
        trigger.focus();
    });
}

function initSidebarHealth() {
    const box = document.getElementById('side-health');
    if (!box) return;
    const label = box.querySelector('.side-health-label');
    const uptime = document.getElementById('side-uptime');
    const lastPoll = document.getElementById('side-last-poll');
    const environment = document.getElementById('ops-environment');
    const environmentLabel = document.getElementById('ops-environment-label');

    // Human-readable names for the background worker processes that report
    // liveness via /api/health, so the sidebar shows what is actually running.
    const WORKER_LABELS = {
        pipeline_worker: 'Pipelines',
        iac_worker: 'IaC',
    };

    function friendly(name) {
        return WORKER_LABELS[name] || name;
    }

    function ago(iso) {
        if (!iso) return 'no signal';
        const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
        if (secs < 60) return 'just now';
        if (secs < 3600) return Math.round(secs / 60) + 'm ago';
        if (secs < 86400) return Math.round(secs / 3600) + 'h ago';
        return Math.round(secs / 86400) + 'd ago';
    }

    function formatUptime(seconds) {
        const value = Math.max(0, Number(seconds) || 0);
        const days = Math.floor(value / 86400);
        const hours = Math.floor((value % 86400) / 3600);
        const minutes = Math.floor((value % 3600) / 60);
        if (days) return `${days}d ${hours}h`;
        if (hours) return `${hours}h ${minutes}m`;
        return `${minutes}m`;
    }

    function apply(state, text, title) {
        box.classList.remove('ok', 'warn', 'down');
        box.classList.add(state);
        if (label) label.textContent = text;
        box.setAttribute('title', title || text);
        if (environment) {
            environment.classList.remove('ok', 'warn', 'down');
            environment.classList.add(state);
            environment.setAttribute('title', title || text);
        }
        if (environmentLabel) {
            const stateLabel = state === 'ok' ? 'LIVE' : state === 'warn' ? 'DEGRADED' : 'UNAVAILABLE';
            environmentLabel.textContent = `INTERNAL · ${stateLabel}`;
            environment?.setAttribute('aria-label', `Current environment: ${stateLabel.toLowerCase()}`);
        }
    }

    function poll() {
        if (document.hidden) return;
        fetch('/api/health', { cache: 'no-store' })
            .then((r) => r.ok ? r.json() : Promise.reject(new Error(String(r.status))))
            .then((data) => {
                const workers = data.workers || {};
                const names = Object.keys(workers);
                const tip = names.length
                    ? 'Background workers:\n' + names
                          .map((n) => '• ' + friendly(n) + ': ' + (workers[n].stale ? 'stale' : 'online') + ' (' + ago(workers[n].last_beat_at) + ')')
                          .join('\n')
                    : 'No workers registered';
                const reasons = Array.isArray(data.degraded_reasons) ? data.degraded_reasons : [];
                const stale = names.filter((n) => workers[n].stale);
                const degradedTip = reasons.length ? reasons.map((reason) => `• ${reason}`).join('\n') : tip;
                if (data.operational_status === 'degraded') {
                    apply('warn', 'Runtime degraded', degradedTip);
                } else if (stale.length) {
                    apply('warn', `${stale.length} worker${stale.length === 1 ? '' : 's'} stale`, tip);
                } else if (names.length) {
                    apply('ok', `${names.length} worker${names.length === 1 ? '' : 's'} online`, tip);
                } else {
                    apply('ok', 'API online', tip);
                }
                if (uptime) uptime.textContent = formatUptime(data.uptime_seconds);
                if (lastPoll) lastPoll.textContent = data.last_pipeline_poll_at ? ago(data.last_pipeline_poll_at) : 'No polls yet';
            })
            .catch(() => apply('down', 'API unavailable', 'Could not reach /api/health'));
    }

    poll();
    setInterval(poll, 60000);
}

function addInteractiveHandlers() {
    // Add click handlers for status badges to filter data
    const statusBadges = document.querySelectorAll('.status-badge');
    statusBadges.forEach(badge => {
        badge.addEventListener('click', function() {
            const status = this.textContent.trim();
            filterByStatus(status);
        });
    });

    // Add search functionality to tables
    addTableSearch();
}

function filterByStatus(status) {
    const rows = document.querySelectorAll('.data-table tbody tr');
    rows.forEach(row => {
        const statusCell = row.querySelector('.status-badge');
        if (statusCell && statusCell.textContent.includes(status)) {
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    });
}

function addTableSearch() {
    if (document.querySelector('.filter-bar, .scan-target-form')) return;
    const tables = document.querySelectorAll('.data-table');
    tables.forEach((table, index) => {
        if (table.previousElementSibling?.classList.contains('table-search')) return;
        const section = table.closest('section, .table-container, .dashboard-section');
        const heading = section?.querySelector('h1, h2, h3, caption')?.textContent.trim();
        const searchInput = document.createElement('input');
        searchInput.type = 'search';
        searchInput.className = 'table-search';
        searchInput.placeholder = 'Search table…';
        searchInput.setAttribute('aria-label', heading ? `Search ${heading}` : `Search table ${index + 1}`);

        const scrollRegion = table.closest('[data-responsive-table-scroll]');
        const container = scrollRegion ? scrollRegion.parentElement : table.parentElement;
        container.insertBefore(searchInput, scrollRegion || table);

        searchInput.addEventListener('input', function() {
            const searchTerm = this.value.toLowerCase();
            const rows = table.querySelectorAll('tbody tr');

            rows.forEach(row => {
                const text = row.textContent.toLowerCase();
                if (text.includes(searchTerm)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            });
        });
    });
}

function setupResponsiveTables() {
    document.querySelectorAll('[data-responsive-table]').forEach(root => {
        const scrollRegion = root.querySelector('[data-responsive-table-scroll]');
        const hint = root.querySelector('.responsive-table-hint');
        if (!scrollRegion) return;

        const updateState = () => {
            const hasOverflow = scrollRegion.scrollWidth > scrollRegion.clientWidth + 1;
            const atStart = scrollRegion.scrollLeft <= 1;
            const atEnd = (
                scrollRegion.scrollLeft + scrollRegion.clientWidth
                >= scrollRegion.scrollWidth - 1
            );

            root.classList.toggle('has-overflow', hasOverflow);
            root.classList.toggle('at-start', atStart);
            root.classList.toggle('at-end', atEnd);
            if (hint) hint.hidden = !hasOverflow;
        };

        scrollRegion.addEventListener('scroll', updateState, { passive: true });
        if (typeof ResizeObserver === 'function') {
            const observer = new ResizeObserver(updateState);
            observer.observe(scrollRegion);
            const table = scrollRegion.querySelector('.data-table');
            if (table) observer.observe(table);
        } else {
            window.addEventListener('resize', updateState);
        }
        updateState();
    });
}

function setupAuditProductPicker() {
    const root = document.querySelector('[data-audit-product-select]');
    if (!root) return;

    const trigger = root.querySelector('[data-product-trigger]');
    const panel = root.querySelector('[data-product-panel]');
    const search = root.querySelector('[data-product-search]');
    const options = Array.from(root.querySelectorAll('.audit-product-option'));
    const checkboxes = Array.from(root.querySelectorAll('[data-product-checkbox]'));
    const summary = root.querySelector('[data-product-summary]');
    const selectVisibleBtn = root.querySelector('[data-product-select-visible]');
    const clearAllBtn = root.querySelector('[data-product-clear]');
    const applyBtn = root.querySelector('[data-product-apply]');

    if (!trigger || !panel || !summary) return;

    const updateSummary = () => {
        const selected = checkboxes.filter(cb => cb.checked).length;
        summary.textContent = selected === 0 ? 'All monitored products' : `${selected} selected`;
    };

    trigger.addEventListener('click', () => {
        root.classList.toggle('open');
        trigger.setAttribute('aria-expanded', root.classList.contains('open') ? 'true' : 'false');
        if (root.classList.contains('open') && search) {
            search.focus();
        }
    });

    if (search) {
        search.addEventListener('input', () => {
            const q = (search.value || '').trim().toLowerCase();
            options.forEach(option => {
                const name = option.dataset.productName || '';
                option.style.display = !q || name.includes(q) ? '' : 'none';
            });
        });
    }

    checkboxes.forEach(cb => cb.addEventListener('change', updateSummary));

    if (selectVisibleBtn) {
        selectVisibleBtn.addEventListener('click', () => {
            options.forEach(option => {
                if (option.style.display === 'none') return;
                const cb = option.querySelector('[data-product-checkbox]');
                if (cb) cb.checked = true;
            });
            updateSummary();
        });
    }

    if (clearAllBtn) {
        clearAllBtn.addEventListener('click', () => {
            checkboxes.forEach(cb => { cb.checked = false; });
            updateSummary();
        });
    }

    if (applyBtn) {
        applyBtn.addEventListener('click', () => {
            root.classList.remove('open');
            trigger.setAttribute('aria-expanded', 'false');
        });
    }

    document.addEventListener('click', (event) => {
        if (!root.contains(event.target)) {
            root.classList.remove('open');
            trigger.setAttribute('aria-expanded', 'false');
        }
    });

    updateSummary();
}

function setupAuditScanStatus() {
    const root = document.querySelector('[data-audit-scan-status]');
    const submitBtn = document.querySelector('[data-run-scan-submit]');
    const runForm = submitBtn ? submitBtn.closest('form') : null;
    if (!root) {
        if (runForm && submitBtn) {
            runForm.addEventListener('submit', () => {
                submitBtn.disabled = true;
                submitBtn.classList.add('loading');
                const text = submitBtn.querySelector('span');
                if (text) text.textContent = 'Starting...';
            });
        }
        return;
    }

    const runId = root.dataset.runId;
    const title = root.querySelector('[data-scan-status-title]');
    const message = root.querySelector('[data-scan-status-text]');
    const progressMeter = root.querySelector('[data-scan-progress]');
    const progressFill = root.querySelector('[data-scan-progress-bar]');
    const progressText = root.querySelector('[data-scan-progress-text]');
    const liveRegion = root.querySelector('[data-scan-live-region]');
    let pollTimer = null;
    let reloadTimer = null;
    const currentUrl = new URL(window.location.href);
    const shouldAutoFinalize = currentUrl.searchParams.get('scan_started') === 'true';

    const setSubmitState = (isBusy) => {
        if (!submitBtn) return;
        submitBtn.disabled = isBusy;
        const text = submitBtn.querySelector('span');
        if (!text) return;
        text.textContent = isBusy ? 'Scan in progress...' : 'Run Scan';
    };

    const applyRunState = (run) => {
        if (!run) return;
        const status = run.status || 'unknown';
        const isBusy = status === 'running' || status === 'queued';
        root.dataset.runStatus = status;
        root.classList.remove('running', 'failed');
        if (isBusy) {
            root.classList.add('running');
        } else if (status === 'failed') {
            root.classList.add('failed');
        }

        if (title) {
            if (status === 'running') title.textContent = 'Scan in progress';
            else if (status === 'queued') title.textContent = 'Scan queued';
            else if (status === 'cancelled') title.textContent = 'Scan cancelled';
            else if (status === 'failed') title.textContent = 'Scan failed';
            else title.textContent = 'Last scan run';
        }
        if (message) {
            message.textContent = run.message || 'Waiting for status updates...';
        }
        const total = Math.max(0, Number(run.total_repositories || 0) || 0);
        const done = Math.max(0, Number(run.processed_repositories || 0) || 0);
        const pct = total > 0 ? Math.min(100, Math.round((done / total) * 1000) / 10) : 0;
        if (progressFill) progressFill.style.width = `${pct}%`;
        if (progressMeter) {
            progressMeter.setAttribute('aria-valuenow', String(pct));
            progressMeter.setAttribute(
                'aria-valuetext',
                total > 0
                    ? `${done} of ${total} repositories processed`
                    : 'Waiting for repository count',
            );
        }
        if (progressText) progressText.textContent = `${done} / ${total}`;
        if (liveRegion) {
            const statusLabel = title ? title.textContent.trim() : 'Scan status updated';
            const statusMessage = message ? message.textContent.trim() : '';
            const progressLabel = isBusy
                ? (total > 0 ? `${done} of ${total} repositories processed` : 'Waiting for repository count')
                : '';
            const announcement = [statusLabel, statusMessage, progressLabel]
                .filter(Boolean)
                .join('. ');
            if (liveRegion.textContent.trim() !== announcement) {
                liveRegion.textContent = announcement;
            }
        }
        setSubmitState(isBusy);
    };

    const poll = async () => {
        try {
            const query = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
            const response = await fetch(`/audit/scan-status${query}`, { cache: 'no-store' });
            if (!response.ok) return;
            const payload = await response.json();
            const run = payload && payload.run ? payload.run : null;
            if (!run) return;
            applyRunState(run);

            if (run.status === 'completed' || run.status === 'failed' || run.status === 'cancelled') {
                if (pollTimer) {
                    clearInterval(pollTimer);
                    pollTimer = null;
                }
                if (run.status === 'completed' && shouldAutoFinalize && !reloadTimer) {
                    reloadTimer = setTimeout(() => {
                        const cleanUrl = new URL(window.location.href);
                        cleanUrl.searchParams.delete('scan_started');
                        cleanUrl.searchParams.delete('run_id');
                        window.location.replace(cleanUrl.toString());
                    }, 700);
                }
            }
        } catch (err) {
            console.error('audit status polling failed', err);
        }
    };

    applyRunState({
        status: root.dataset.runStatus || (root.classList.contains('running') ? 'running' : 'completed'),
        message: message ? message.textContent : '',
        total_repositories: progressText ? Number((progressText.textContent.split('/')[1] || '').trim() || 0) : 0,
        processed_repositories: progressText ? Number((progressText.textContent.split('/')[0] || '').trim() || 0) : 0,
    });
    poll();
    pollTimer = setInterval(poll, 2500);

    if (runForm && submitBtn) {
        runForm.addEventListener('submit', () => {
            setSubmitState(true);
        });
    }
}

function setupFilterClearButtons() {
    document.querySelectorAll('[data-clear-filter]').forEach(button => {
        button.addEventListener('click', () => {
            const inputId = button.getAttribute('data-clear-filter');
            const input = inputId ? document.getElementById(inputId) : null;
            if (!input) return;
            input.value = '';
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
        });
    });
}

function setupCopyButtons() {
    document.querySelectorAll('[data-copy-target]').forEach(button => {
        button.addEventListener('click', async () => {
            const target = document.getElementById(button.dataset.copyTarget || '');
            const feedback = document.getElementById(button.dataset.copyFeedback || '');
            if (!target) return;

            const value = target.textContent || '';
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(value);
                } else {
                    const textarea = document.createElement('textarea');
                    textarea.value = value;
                    textarea.setAttribute('readonly', '');
                    textarea.style.position = 'fixed';
                    textarea.style.opacity = '0';
                    document.body.appendChild(textarea);
                    textarea.select();
                    const copied = document.execCommand('copy');
                    textarea.remove();
                    if (!copied) throw new Error('Copy command was rejected');
                }
                if (feedback) feedback.textContent = 'Copied';
            } catch (error) {
                if (feedback) feedback.textContent = 'Copy failed';
                console.error('Unable to copy diagnostic', error);
            }
        });
    });
}

// ---- Autocomplete for logs filters ----
function submitFilterForm(container) {
    const form = container.closest('form');
    if (!form) return;
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
}

function setupLogsAutocomplete() {
    const autocompletes = document.querySelectorAll('.autocomplete');
    if (!autocompletes.length) return;

    autocompletes.forEach(container => {
        const input = container.querySelector('input');
        const suggestionsBox = container.querySelector('.suggestions');
        const clearButton = container.querySelector('[data-clear-filter]');
        const type = container.dataset.type; // 'repo' or 'job'
        const endpoint = container.dataset.endpoint;
        const onlyFailedParam = container.dataset.onlyFailed;
        const categoryParam = container.dataset.category;
        const timeframeParam = container.dataset.timeframe;
        let items = [];
        let selectedIndex = -1;

        // fetch suggestions once on focus
        async function ensureItems() {
            if (items.length) return;
            // Use preloaded suggestions from server-rendered data if available
            const pre = container.dataset.suggestions;
            if (pre) {
                try {
                    const parsed = JSON.parse(pre);
                    if (Array.isArray(parsed)) {
                        items = parsed;
                        return;
                    }
                    // if object, pick property by type
                    if (typeof parsed === 'object') {
                        items = (type === 'repo') ? (parsed.repos || []) : (parsed.jobs || []);
                        return;
                    }
                } catch (e) {
                    // fallback to fetch
                }
            }

            try {
                let url = endpoint;
                const params = new URLSearchParams();
                if (onlyFailedParam !== undefined) {
                    params.set('only_failed', onlyFailedParam);
                }
                if (categoryParam) {
                    params.set('category', categoryParam);
                }
                if (timeframeParam) {
                    params.set('timeframe', timeframeParam);
                }
                const query = params.toString();
                if (query) {
                    url = `${endpoint}?${query}`;
                }

                const res = await fetch(url);
                if (!res.ok) return;
                const data = await res.json();
                items = (type === 'repo') ? (data.repos || []) : (data.jobs || []);
            } catch (e) {
                console.error('Failed to load suggestions', e);
            }
        }

        function renderSuggestions(filtered) {
            suggestionsBox.innerHTML = '';
            if (!filtered.length) {
                suggestionsBox.style.display = 'none';
                suggestionsBox.setAttribute('aria-hidden', 'true');
                return;
            }
            filtered.forEach((it, idx) => {
                const div = document.createElement('div');
                div.className = 'suggestion-item';
                div.textContent = it;
                div.dataset.index = idx;
                div.addEventListener('mousedown', (e) => {
                    e.preventDefault();
                    input.value = it;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    hide();
                    if (container.dataset.autoApply === 'true') submitFilterForm(container);
                });
                suggestionsBox.appendChild(div);
            });
            selectedIndex = -1;
            suggestionsBox.style.display = 'block';
            suggestionsBox.setAttribute('aria-hidden', 'false');
        }

        function hide() {
            suggestionsBox.innerHTML = '';
            suggestionsBox.style.display = 'none';
            suggestionsBox.setAttribute('aria-hidden', 'true');
        }

        function filterItems(term) {
            if (!term) return items.slice(0, 12);
            const q = term.toLowerCase();
            return items.filter(i => i && i.toLowerCase().includes(q)).slice(0, 12);
        }

        input.addEventListener('focus', async () => {
            await ensureItems();
            if (input.value) {
                input.select();
            }
            const list = items.slice(0, 12);
            renderSuggestions(list);
        });

        if (clearButton) {
            clearButton.addEventListener('click', async () => {
                await ensureItems();
                renderSuggestions(items.slice(0, 12));
            });
        }

        input.addEventListener('input', async () => {
            await ensureItems();
            const list = filterItems(input.value || '');
            renderSuggestions(list);
        });

        input.addEventListener('keydown', (e) => {
            const itemsEls = suggestionsBox.querySelectorAll('.suggestion-item');
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (itemsEls.length === 0) return;
                selectedIndex = Math.min(selectedIndex + 1, itemsEls.length - 1);
                updateHighlight(itemsEls);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (itemsEls.length === 0) return;
                selectedIndex = Math.max(selectedIndex - 1, 0);
                updateHighlight(itemsEls);
            } else if (e.key === 'Enter') {
                if (selectedIndex >= 0 && itemsEls[selectedIndex]) {
                    e.preventDefault();
                    const val = itemsEls[selectedIndex].textContent;
                    input.value = val;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    hide();
                    if (container.dataset.autoApply === 'true') submitFilterForm(container);
                }
            } else if (e.key === 'Escape') {
                hide();
            }
        });

        function updateHighlight(itemsEls) {
            itemsEls.forEach((el, idx) => {
                if (idx === selectedIndex) {
                    el.classList.add('active');
                    el.scrollIntoView({ block: 'nearest' });
                } else {
                    el.classList.remove('active');
                }
            });
        }

        // hide on outside click
        document.addEventListener('click', (e) => {
            if (!container.contains(e.target)) hide();
        });
    });
}

// initialize autocompletes on DOMContentLoaded
document.addEventListener('DOMContentLoaded', function() {
    try { setupLogsAutocomplete(); } catch (e) { console.error(e); }
});

// Utility functions
function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString();
}

function getStatusColor(status) {
    const colors = {
        'success': '#10b981',
        'failed': '#ef4444',
        'running': '#34d399',
        'cancelled': '#f59e0b',
        'skipped': '#94a3b8'
    };
    return colors[status] || '#94a3b8';
}

// Export functions for use in templates
window.SecurityDashboard = {
    formatTimestamp,
    getStatusColor,
    filterByStatus
};
