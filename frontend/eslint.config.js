import js from '@eslint/js'
import vue from 'eslint-plugin-vue'
import globals from 'globals'

export default [
  { ignores: ['dist/**'] },
  { ...js.configs.recommended, files: ['**/*.{js,mjs,vue}'] },
  ...vue.configs['flat/essential'],
  { files: ['src/**/*.{js,mjs,vue}'], languageOptions: { globals: globals.browser } },
  { files: ['*.{js,mjs}', 'tests/**/*.mjs'], languageOptions: { globals: globals.node } },
]
