/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

/** 计算文件全文 SHA-256 指纹（十六进制），与后端 hashlib.sha256 口径一致，
 * 用于「按指纹幂等」：同一文件重传后端直接返回首次结果。 */
export async function sha256Hex(content: string): Promise<string> {
  const buffer = new TextEncoder().encode(content)
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/** 把整份文件按行拆成续传分片（含表头行），offset 为行偏移量。 */
export function splitIntoLines(content: string): string[] {
  return content.replace(/^﻿/, '').split(/\r?\n/)
}
