/**
 * 预加载脚本 —— 在隔离的上下文里，把主进程能力安全地暴露给网页。
 *
 * 网页只能看到 window.wxbotDesktop 上这几个方法，
 * 拿不到 require / process / fs，因此即便界面里加载了不可信内容也安全。
 */
'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/** 主进程 → 渲染进程的单向事件（后端崩溃、重启等） */
const EVENTS = ['backend:crashed', 'backend:restarted', 'backend:ready'];

contextBridge.exposeInMainWorld('wxbotDesktop', {
  /** 是否运行在桌面应用里（网页用它决定是否显示桌面专属功能） */
  isDesktop: true,

  info: () => ipcRenderer.invoke('app:info'),

  hide: () => ipcRenderer.invoke('app:hide'),
  quit: () => ipcRenderer.invoke('app:quit'),

  data: { chooseLegacyDir: () => ipcRenderer.invoke('data:chooseLegacyDir') },
  companion: {
    show:()=>ipcRenderer.invoke('companion:show'), hide:()=>ipcRenderer.invoke('companion:hide'),
    reload:()=>ipcRenderer.invoke('companion:reload'),
    top:value=>ipcRenderer.invoke('companion:top',value),through:value=>ipcRenderer.invoke('companion:through',value),
    chooseModel:()=>ipcRenderer.invoke('companion:chooseModel'),
  },

  backend: {
    status: () => ipcRenderer.invoke('backend:status'),
    restart: () => ipcRenderer.invoke('backend:restart'),
    log: (lines) => ipcRenderer.invoke('backend:log', lines),
  },

  shell: {
    openDataDir: () => ipcRenderer.invoke('shell:openDataDir'),
    openLogDir: () => ipcRenderer.invoke('shell:openLogDir'),
    openExternal: (url) => ipcRenderer.invoke('shell:openExternal', url),
  },

  /** 订阅主进程事件，返回取消订阅函数 */
  on: (channel, handler) => {
    if (!EVENTS.includes(channel)) return () => {};
    const wrapped = (_e, payload) => handler(payload);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
});
