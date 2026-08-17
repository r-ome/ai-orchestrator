## Explanation style

When explaining a codebase, architecture, or technical concept:

- Start with the **big picture** before discussing individual files, functions, or implementation details.
- First explain what the system is trying to do, its major components, and how those components interact.
- Prefer explaining the **flow of the system** end-to-end, such as how a request, event, or piece of data moves through the codebase.
- Introduce technical jargon when it is useful, but do not overload the explanation with unfamiliar terminology. Define important terms in plain language when they first appear.
- Use intuitive analogies to explain technical concepts, but always connect the analogy back to the actual implementation.
- Explain **why** a component, abstraction, or architectural boundary exists, not only what it does.
- Prioritize the parts of the codebase that are important for understanding the architecture. Explicitly mention which details or directories can be ignored initially.
- Move progressively from:
  1. mental model,
  2. major components and relationships,
  3. important terminology,
  4. concrete files/classes/functions,
  5. deeper implementation details.

- Do not simplify by removing important technical nuance. Instead, make the path to understanding the technical details easier.
- Assume the reader is technically capable but unfamiliar with this particular codebase.

When useful, explain things in the form:

**Big picture → analogy/intuition → technical explanation → concrete code references.**
