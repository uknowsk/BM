// set theme before first paint to avoid a flash (external file: the CSP forbids inline scripts)
try { var t = localStorage.getItem('gauge.theme'); if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t; } catch (e) {}
