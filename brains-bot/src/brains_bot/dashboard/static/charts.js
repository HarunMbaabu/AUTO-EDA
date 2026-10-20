// Draws the overview charts from the JSON block on the page. Colours come from CSS tokens,
// so light and dark mode each use their own validated steps.
(function () {
  const el = document.getElementById("chart-data");
  if (!el || !window.Chart) return;
  const data = JSON.parse(el.textContent);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const ink = css("--text-secondary"), grid = css("--grid"), surface = css("--surface-1");
  const series = [css("--series-1"), css("--series-2"), css("--series-3")];
  Chart.defaults.color = ink;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;

  function horizontalBars(id, chartData, stacked) {
    const canvas = document.getElementById(id);
    if (!canvas || !chartData.labels.length) return;
    new Chart(canvas, {
      type: "bar",
      data: {
        labels: chartData.labels,
        datasets: chartData.series.map((s, i) => ({
          label: s.label,
          data: s.values,
          backgroundColor: series[i],
          borderColor: surface,
          borderWidth: { right: 2, left: 0, top: 0, bottom: 0 },
          borderRadius: 4,
          borderSkipped: "start",
          maxBarThickness: 18,
        })),
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        interaction: { mode: "index", axis: "y", intersect: false },
        plugins: {
          legend: { position: "top", align: "start", labels: { boxWidth: 12, boxHeight: 12 } },
          tooltip: { callbacks: { footer: (items) =>
            stacked ? "Total: " + items.reduce((n, i) => n + i.parsed.x, 0) : "" } },
        },
        scales: {
          x: { stacked, beginAtZero: true, grid: { color: grid }, ticks: { precision: 0 } },
          y: { stacked, grid: { display: false } },
        },
      },
    });
  }
  horizontalBars("pipelineChart", data.pipeline, true);
  horizontalBars("pitchedChart", data.pitched, false);
})();
