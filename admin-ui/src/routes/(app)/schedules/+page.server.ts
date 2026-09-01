import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';

/** Расписания переехали на «Запуски».
 *
 * Страница осталась ради закладок и ссылок из переписки: обе половины —
 * расписания и история прогонов — теперь на одном экране, и держать здесь их
 * половину значило бы держать два ответа на один вопрос.
 */
export const load: PageServerLoad = async () => {
	redirect(308, '/runs');
};
