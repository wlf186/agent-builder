import { expect, test } from "@playwright/test";

test("project-local runtime reports its availability through the authenticated proxy", async ({
  request,
}) => {
  const response = await request.get("/api/system/check-runtime");
  expect(response.ok()).toBeTruthy();
  const result = await response.json();

  expect(result).toEqual(
    expect.objectContaining({
      available: expect.any(Boolean),
      message: expect.any(String),
    }),
  );
  expect(result).toHaveProperty("path");
  expect(result).toHaveProperty("version");
  expect(result).toHaveProperty("error");
  if (result.available) {
    expect(result.path).toContain(".tools/uv");
    expect(result.version).toContain("uv");
    expect(result.error).toBeNull();
  }
});
