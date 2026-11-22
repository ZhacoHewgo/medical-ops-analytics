import html2canvas from 'html2canvas'
import { jsPDF } from 'jspdf'

function normalizeColors(report) {
  const swatch = document.createElement('canvas')
  swatch.width = swatch.height = 1
  const context = swatch.getContext('2d', { willReadFrequently: true })
  function rgb(color) {
    context.clearRect(0, 0, 1, 1)
    context.fillStyle = color
    context.fillRect(0, 0, 1, 1)
    const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data
    return `rgba(${r}, ${g}, ${b}, ${a / 255})`
  }
  const styles = getComputedStyle(report)
  for (const name of ['--border', '--muted', '--surface-tint']) {
    report.style.setProperty(name, rgb(styles.getPropertyValue(name).trim()))
  }
  report.style.boxShadow = styles.boxShadow.replace(/color\([^)]*\)/g, rgb)
}

export async function downloadReportPdf(report, filename, signal) {
  signal.throwIfAborted()
  const copy = report.cloneNode(true)
  copy.setAttribute('aria-hidden', 'true')
  copy.inert = true
  copy.style.cssText = 'position:absolute;left:-10000px;top:0;margin:0;width:720px;padding:32px'
  copy.querySelectorAll('button, .chart-table').forEach(node => node.remove())
  document.body.append(copy)
  let frame
  const cleanUp = () => {
    copy.remove()
    frame?.remove()
  }
  signal.addEventListener('abort', cleanUp, { once: true })

  try {
    await document.fonts.ready
    signal.throwIfAborted()
    normalizeColors(copy)
    for (const figure of copy.querySelectorAll('.chart-snapshot')) {
      const image = figure.querySelector('img')
      if (!image) throw new Error('Chart unavailable')
      await image.decode()
      signal.throwIfAborted()
      const chart = figure.querySelector('.chart')
      image.removeAttribute('hidden')
      const ratio = image.naturalWidth / image.naturalHeight
      const width = Math.min(figure.clientWidth, chart.clientHeight * ratio)
      chart.remove()
      image.style.cssText = `position:static;display:block;margin:auto;width:${width}px;height:${width / ratio}px;animation:none`
    }

    const top = copy.getBoundingClientRect().top
    const breaks = [...copy.querySelectorAll(':scope > section')].map(section => Math.round(section.getBoundingClientRect().top - top - 12))
    const frames = () => [...document.querySelectorAll('iframe.html2canvas-container')]
    const previousFrames = frames()
    const capture = html2canvas(copy, {
      scale: 2,
      logging: false,
      removeContainer: false,
      backgroundColor: '#ffffff',
      ignoreElements: element => element.id === 'app',
    })
    frame = frames().find(element => !previousFrames.includes(element))
    const canvas = await capture
    signal.throwIfAborted()
    const pdf = new jsPDF({ format: 'a4', unit: 'mm', compress: true })
    const margin = 10
    const pageWidth = pdf.internal.pageSize.getWidth() - 2 * margin
    const pageHeight = pdf.internal.pageSize.getHeight() - 2 * margin
    const pageLimit = Math.floor(canvas.width * pageHeight / pageWidth)
    const ends = [...breaks.map(value => value * 2), canvas.height]
    const page = document.createElement('canvas')
    page.width = canvas.width

    for (let start = 0; start < canvas.height;) {
      const end = ends.findLast(value => value > start && value <= start + pageLimit) ?? ends.find(value => value > start)
      page.height = end - start
      page.getContext('2d').drawImage(canvas, 0, start, canvas.width, page.height, 0, 0, page.width, page.height)
      const scale = Math.min(pageWidth / page.width, pageHeight / page.height)
      const width = page.width * scale
      if (start) pdf.addPage()
      pdf.addImage(page, 'PNG', margin + (pageWidth - width) / 2, margin, width, page.height * scale)
      start = end
    }

    pdf.save(filename)
  } finally {
    signal.removeEventListener('abort', cleanUp)
    cleanUp()
  }
}
