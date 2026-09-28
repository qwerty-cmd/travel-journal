import { afterEach, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

// Proves the runner works end to end: TSX compiles, jsdom provides a DOM,
// and React renders into it.
afterEach(cleanup);

function Greeting({ name }: { name: string }) {
  return <p>Hello, {name}</p>;
}

test("renders a React component into jsdom", () => {
  render(<Greeting name="rider" />);
  expect(screen.getByText("Hello, rider")).toBeTruthy();
});
