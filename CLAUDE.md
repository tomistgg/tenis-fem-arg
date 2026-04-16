Workflow Orchestration

1. Plan Node Default
• Enter plan mode for any non-trivial task (three or more steps, or involving architectural decisions).
• If something goes wrong, stop and re-plan immediately rather than continuing blindly.
• Use plan mode for verification steps, not just implementation.
• Write detailed specifications upfront to reduce ambiguity.

2. Verification Before Done
• Never mark a task complete without proving it works.
• Diff behavior between main and your changes when relevant.
• Ask: “Would a staff engineer approve this?”
• Run tests, check logs, and demonstrate correctness.

4. Demand Elegance (Balanced)
• For non-trivial changes, pause and ask whether there is a more elegant solution.
• If a fix feels hacky, implement the solution you would choose knowing everything you now know.
• Do not over-engineer simple or obvious fixes.
• Critically evaluate your own work before presenting it.

Core Principles

• Simplicity First: Make every change as simple as possible. Minimize code impact.
• No Laziness: Identify root causes. Avoid temporary fixes. Apply senior developer standards.
• Minimal Impact: Touch only what is necessary. Avoid introducing new bugs.
• Readability: Make code easy to understand
• Maintainability: Write code that's easy to update
• Reusability: Create reusable components and functions

Coding Best Practices

• Early Returns: Use to avoid nested conditions
• Constants Over Functions: Use constants where possible
• DRY Code: Don't repeat yourself
• Functional Style: Prefer functional, immutable approaches when not verbose
• Minimal Changes: Only modify code related to the task at hand
• Function Ordering: Define composing functions before their components
• TODO Comments: Mark issues in existing code with "TODO:" prefix
• Simplicity: Prioritize simplicity and readability over clever solutions
• Build Iteratively: Start with minimal functionality and verify it works before adding complexity
• Run Tests: Test your code frequently with realistic inputs and validate outputs.