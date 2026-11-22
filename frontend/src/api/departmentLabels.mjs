const departments = new Map([
  ['EK', '儿科'],
  ['FC', '妇产科'],
  ['GK', '骨科'],
  ['HN', '心血管内科'],
  ['HX', '呼吸内科'],
  ['ICU', '重症医学科'],
  ['JZ', '急诊科'],
  ['PW', '普外科'],
  ['SJ', '神经内科'],
  ['XH', '消化内科'],
  ['__UNKNOWN__', '未标注科室'],
])

export function outpatientDepartmentName(code) {
  return departments.get(code) ?? code
}

const departmentChartColors = new Map([
  ['EK', '#6A9FF8'], ['FC', '#7EA7ED'], ['GK', '#5DAFCF'], ['HN', '#1958FD'], ['HX', '#3BA99C'],
  ['ICU', '#67C5B8'], ['JZ', '#8FC7D8'], ['PW', '#3F7BFA'], ['SJ', '#FF9671'], ['XH', '#F27D5F'],
])

export function departmentChartColor(code) {
  return departmentChartColors.get(code) ?? '#66758C'
}
