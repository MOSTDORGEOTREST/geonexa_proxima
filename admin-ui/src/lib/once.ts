/**
 * Отправка формы, которую нельзя повторить двойным кликом.
 *
 * `use:enhance` без аргументов не блокирует кнопку: второй клик по «Продлить»
 * шлёт второй POST, а продление аддитивное — подписка уезжает на 60 дней
 * вместо 30. На «Запусках» так же ставятся два одинаковых прогона в Prefect.
 *
 * Кнопка гасится на время запроса и возвращается по ответу — включая ошибку,
 * иначе после неудачи форму нельзя было бы отправить повторно.
 */

import { enhance } from '$app/forms';
import type { SubmitFunction } from '@sveltejs/kit';

export function once(form: HTMLFormElement) {
	const submit: SubmitFunction = ({ cancel }) => {
		if (form.dataset.busy === '1') {
			cancel();
			return;
		}
		form.dataset.busy = '1';
		for (const button of form.querySelectorAll('button')) button.disabled = true;
		return async ({ update }) => {
			await update({ reset: false });
			form.dataset.busy = '';
			for (const button of form.querySelectorAll('button')) button.disabled = false;
		};
	};
	return enhance(form, submit);
}
