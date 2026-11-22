<script setup>
defineProps({ name: { type: String, required: true } })

const paths = {
  home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',
  people: 'M15 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-4-4m-6-5a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0Zm6-3a3 3 0 0 1 0 6',
  bed: 'M3 20V6m18 14v-7a3 3 0 0 0-3-3h-5v6M3 16h18M8 13a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
  money: 'M20 6c0 2-3.6 3.5-8 3.5S4 8 4 6s3.6-3.5 8-3.5S20 4 20 6Zm-16 0v12c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5V6M4 12c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5',
  pulse: 'M3 12h4l2.5-6 4 12 2.5-6h5M4 5a9.8 9.8 0 1 1 0 14',
  resource: 'M9 15 15 9m-7 5H6.5a3.5 3.5 0 1 1 0-7H11m2 10h4.5a3.5 3.5 0 1 0 0-7H16',
  file: 'M6 3h9l4 4v14H5V4a1 1 0 0 1 1-1Zm8 0v5h5M8 12h8m-8 4h8',
  grid: 'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z',
  chart: 'M4 3v17h17M8 16v-4m5 4V8m5 8V5',
  question: 'M20 11a8 8 0 0 1-8 8H5l1-4a8 8 0 1 1 14-4Zm-10-3a2 2 0 1 1 3 1.7c-.8.4-1 1-1 1.3m0 3h.01',
  user: 'M5 21v-2a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v2Zm11-13a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  storage: 'M4 4h16v7H4zm0 9h16v7H4zm3-5h.01M7 17h.01',
  calendar: 'M4 6h16v15H4zm0 4h16M8 3v5m8-5v5m-8 6h2m4 0h2m-8 3h2',
  department: 'M10 3h4v4h-4zm-7 14h4v4H3zm7 0h4v4h-4zm7 0h4v4h-4zM5 17v-5h14v5m-7-10v10',
}
</script>

<template>
  <svg class="workspace-icon" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path :d="paths[name]" />
  </svg>
</template>
