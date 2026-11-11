(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./ai_settings.js'));
  else {
    const api = root.AIPicker = factory(root.AISettings);
    const previous = root.registerPageComponents;
    root.registerPageComponents = function (app) {
      if (previous) previous(app);
      app.component('ai-model-picker', api.createModelPicker(root.Vue));
    };
  }
})(typeof window === 'object' ? window : globalThis, function (AISettings) {
  'use strict';
  function createModelPicker(Vue) {
    return {
      props: {
        options: {type: Array, default: () => []}, current: {type: Object, default: null},
        label: {type: String, required: true}, id: {type: String, required: true},
        disabled: {type: Boolean, default: false},
      },
      emits: ['select'],
      setup(props, {emit}) {
        const open = Vue.ref(false), query = Vue.ref('');
        const rows = Vue.computed(() => AISettings.filterOptions(
          AISettings.retainCurrent(props.options, props.current), query.value));
        const choose = option => {
          if (props.disabled || !['ready', 'unverified'].includes(option.availability)) return false;
          emit('select', {...option});
          open.value = false;
          return true;
        };
        const show = () => { if (!props.disabled) { query.value = ''; open.value = true; } };
        const close = () => { open.value = false; };
        const moveFocus = (event, direction) => {
          const list = event.currentTarget.closest('[role="listbox"]');
          if (!list) return;
          const buttons = Array.from(list.querySelectorAll('[role="option"]:not([disabled])'));
          const index = buttons.indexOf(event.currentTarget);
          if (buttons.length) buttons[(index + direction + buttons.length) % buttons.length].focus();
        };
        return {open, query, rows, choose, show, close, moveFocus, optionKey: AISettings.optionKey};
      },
      template: `
        <div class="ai-model-picker">
          <button type="button" class="library-btn w-full text-left" :id="id" :disabled="disabled"
            :aria-controls="id + '-dialog'" :aria-expanded="open" aria-haspopup="dialog" @click="show">
            {{ label }}: {{ current ? current.label : '选择模型' }}
          </button>
          <app-modal :is-open="open" :title="label" :id="id + '-dialog'" class="ai-model-picker-dialog" @close="close">
            <div class="ai-model-picker-content">
              <label :for="id + '-filter'" class="block text-sm font-medium">筛选名称、模型 ID、提供方或状态</label>
              <input :id="id + '-filter'" v-model="query" type="search" class="library-input w-full" :aria-controls="id + '-options'">
              <p v-if="current" class="text-xs text-pixiv-gray break-all" role="status">
                当前: {{ current.provider_name }} / {{ current.model_key || current.label }} · {{ current.availability }} {{ current.reason }}
              </p>
              <ul :id="id + '-options'" role="listbox" :aria-label="label" class="ai-model-picker-results">
                <li v-for="(option, index) in rows" :key="optionKey(option)">
                  <button type="button" role="option" class="ai-model-picker-option"
                    :id="id + '-option-' + index" :aria-selected="!!option.current"
                    :aria-disabled="disabled || !['ready', 'unverified'].includes(option.availability)"
                    :disabled="disabled || !['ready', 'unverified'].includes(option.availability)"
                    @click="choose(option)" @keydown.enter.prevent="choose(option)"
                    @keydown.up.prevent="moveFocus($event, -1)" @keydown.down.prevent="moveFocus($event, 1)">
                    <span class="block font-medium break-all">{{ option.label }}</span>
                    <span class="block text-xs break-all">{{ option.provider_name }} <span v-if="option.kind === 'model'">· {{ option.model_key }}</span></span>
                    <span class="block text-xs text-pixiv-gray">{{ option.current ? '当前选择 · ' : '' }}{{ option.availability }} {{ option.reason }}</span>
                  </button>
                </li>
              </ul>
              <p v-if="!rows.length" class="text-sm text-pixiv-gray">没有匹配的可选项。</p>
            </div>
          </app-modal>
        </div>`,
    };
  }
  return {createModelPicker};
});
