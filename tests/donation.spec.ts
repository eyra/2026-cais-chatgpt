import { test, expect, Page } from '@playwright/test';
import * as path from 'path';

// Utrecht formats timestamps in the runtime's local timezone, not UTC.
test.use({ timezoneId: 'Europe/Amsterdam' });

async function uploadChatGPTExport(page: Page): Promise<void> {
  await page.goto('http://localhost:3000/');
  await expect(page.getByRole('heading', { name: 'Your ChatGPT data' })).toBeVisible({ timeout: 90000 });

  const fileChooserPromise = page.waitForEvent('filechooser');
  await page.getByText('Choose file').click();
  const fileChooser = await fileChooserPromise;
  await fileChooser.setFiles(path.join(__dirname, 'test.zip'));
  await page.getByText('Continue').click();

  await expect(page.getByTestId('table-chatgpt_conversations_1')).toBeVisible();
}

function captureDonation(page: Page): Promise<string | null> {
  const { promise, resolve } = Promise.withResolvers<string | null>();
  page.route('/data-submission', async route => {
    await route.fulfill({ json: { ok: true } });
    resolve(route.request().postData());
  });
  return promise;
}
function donatedTables(submittedData: string): Record<string, { data: Record<string, string>[] }> {
  const request = JSON.parse(submittedData);
  return JSON.parse(request.data);
}

test('reviews and submits visible ChatGPT messages from the export', async ({ page }) => {
  await uploadChatGPTExport(page);

  const table = page.getByTestId('table-chatgpt_conversations_1');
  await expect(table.getByText('Newest answer')).toBeVisible();
  await expect(table.getByText('Participant question')).toBeVisible();
  await expect(table.getByText('Hidden answer')).not.toBeVisible();

  const donation = captureDonation(page);
  await page.getByText('Yes, donate', { exact: true }).click();
  const submittedData = await donation;
  expect(submittedData).not.toBeNull();

  expect(donatedTables(submittedData!).chatgpt_conversations_1.data).toEqual([
    {
      'conversation title': 'Research conversation',
      role: 'assistant',
      message: 'Newest answer',
      model: 'gpt-5',
      time: '2100-01-02 13:00:00',
    },
    {
      'conversation title': 'Research conversation',
      role: 'user',
      message: 'Participant question',
      model: '',
      time: '2099-12-01 13:00:00',
    },
  ]);
});

test('removes selected ChatGPT messages before donation', async ({ page }) => {
  await uploadChatGPTExport(page);

  await page.getByRole('checkbox').first().click();
  const table = page.getByTestId('table-chatgpt_conversations_1');
  await table.getByRole('checkbox').nth(1).click();
  await page.getByText('Delete selected').first().click();
  await expect(table.getByText('Newest answer')).not.toBeVisible();

  const donation = captureDonation(page);
  await page.getByText('Yes, donate', { exact: true }).click();
  const submittedData = await donation;
  expect(submittedData).not.toBeNull();

  expect(donatedTables(submittedData!).chatgpt_conversations_1.data).toEqual([
    {
      'conversation title': 'Research conversation',
      role: 'user',
      message: 'Participant question',
      model: '',
      time: '2099-12-01 13:00:00',
    },
  ]);
});

test('rejects an invalid ZIP and returns to file selection', async ({ page }) => {
  await page.goto('http://localhost:3000/');
  await expect(page.getByRole('heading', { name: 'Your ChatGPT data' })).toBeVisible({ timeout: 90000 });

  const fileChooserPromise = page.waitForEvent('filechooser');
  await page.getByText('Choose file').click();
  const fileChooser = await fileChooserPromise;
  await fileChooser.setFiles({
    name: 'not-chatgpt.zip',
    mimeType: 'application/zip',
    buffer: Buffer.from('not a ZIP archive'),
  });
  await page.getByText('Continue').click();

  await expect(page.getByText('We could not verify this as a ChatGPT data export.')).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByText('Choose file')).toBeVisible();
});
