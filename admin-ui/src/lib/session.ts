/**
 * Сессия администратора в httpOnly-cookie.
 *
 * Хранить токен в localStorage означало бы отдать его любому скрипту на
 * странице. Здесь он не покидает сервер: браузер получает лишь cookie,
 * недоступную из JS.
 */

import type { Cookies } from '@sveltejs/kit';

export const ACCESS = 'px_access';
export const REFRESH = 'px_refresh';
export const THEME = 'theme';

const base = {
	path: '/',
	httpOnly: true,
	sameSite: 'lax' as const,
	// В production админка обязана быть за HTTPS; в разработке secure сломал бы вход.
	secure: process.env.NODE_ENV === 'production'
};

export function setSession(cookies: Cookies, access: string, refresh: string, ttl: number): void {
	cookies.set(ACCESS, access, { ...base, maxAge: Math.max(60, ttl) });
	cookies.set(REFRESH, refresh, { ...base, maxAge: 60 * 60 * 24 * 14 });
}

export function clearSession(cookies: Cookies): void {
	cookies.delete(ACCESS, { path: '/' });
	cookies.delete(REFRESH, { path: '/' });
}
