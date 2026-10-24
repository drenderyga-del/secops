/**
 * Repository name filter for config scan results (tree + flat layouts).
 */
(function () {
    function updateCount(root, count) {
        var counter = root.querySelector("[data-cscan-repo-visible]");
        if (counter) counter.textContent = String(count);
    }

    function bindTree(root) {
        var input = root.querySelector(".cscan-repo-search-input");
        if (!input) return;
        function apply() {
            var q = (input.value || "").trim().toLowerCase();
            var allRows = Array.prototype.slice.call(root.querySelectorAll("tr[data-repo-path]"));
            root.querySelectorAll(".cscan-tree-folder").forEach(function (folder) {
                var body = folder.querySelector(".cscan-tree-folder-body");
                if (!body) return;
                var rows = body.querySelectorAll("tbody tr[data-repo-path]");
                var visible = 0;
                rows.forEach(function (tr) {
                    var hay = tr.getAttribute("data-repo-path") || "";
                    var show = !q || hay.indexOf(q) !== -1;
                    tr.style.display = show ? "" : "none";
                    if (show) {
                        visible++;
                    }
                });
                if (rows.length) {
                    folder.style.display = visible ? "" : "none";
                }
            });
            updateCount(root, allRows.filter(function (row) {
                var hay = row.getAttribute("data-repo-path") || "";
                return !q || hay.indexOf(q) !== -1;
            }).length);
        }
        input.addEventListener("input", apply);
        apply();
    }

    function bindFlat(root) {
        var input = root.querySelector(".cscan-repo-search-input");
        if (!input) return;
        function apply() {
            var q = (input.value || "").trim().toLowerCase();
            var totalVisible = 0;
            root.querySelectorAll("[data-cscan-group]").forEach(function (section) {
                var visible = 0;
                section.querySelectorAll("tbody tr[data-repo-path]").forEach(function (tr) {
                    var hay = tr.getAttribute("data-repo-path") || "";
                    var show = !q || hay.indexOf(q) !== -1;
                    tr.style.display = show ? "" : "none";
                    if (show) {
                        visible++;
                        totalVisible++;
                    }
                });
                if (section.classList.contains("cscan-path-group-details")) {
                    section.style.display = visible ? "" : "none";
                }
            });
            updateCount(root, totalVisible);
        }
        input.addEventListener("input", apply);
        apply();
    }

    function init() {
        document.querySelectorAll("[data-cscan-results-tree]").forEach(bindTree);
        document.querySelectorAll("[data-cscan-results]").forEach(bindFlat);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
