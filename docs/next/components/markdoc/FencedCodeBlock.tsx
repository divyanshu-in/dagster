import {Transition} from '@headlessui/react';
import Prism from 'prismjs';
import React from 'react';
import 'prismjs/components/prism-python';
import {useCopyToClipboard} from 'react-use';

Prism.manual = true;

export const Fence = (props) => {
  const text = props.children;
  const language = props['data-language'];
  const [copied, setCopied] = React.useState(false);
  const [state, copy] = useCopyToClipboard();

  React.useEffect(() => {
    Prism.highlightAll();
  }, []);

  const copyToClipboard = React.useCallback(() => {
    if (typeof text === 'string') {
      copy(text);
      setCopied(true);
      setTimeout(() => {
        setCopied(false);
      }, 3000);
    }
  }, [copy, text]);

  return (
    <div className="codeBlock relative" aria-live="polite" style={{display: 'flex'}}>
      <pre className="line-numbers w-full">
        <code className={`language-${language}`}>{text}</code>
      </pre>
      <Transition
        show={!copied}
        appear={true}
        enter="transition ease-out duration-150 transform"
        enterFrom="opacity-0 scale-95"
        enterTo="opacity-100 scale-100"
        leave="transition ease-in duration-150 transform"
        leaveFrom="opacity-100 scale-100"
        leaveTo="opacity-0 scale-95"
      >
        <div className="absolute top-2 right-1 mt-2 mr-2">
          <svg
            className="h-5 w-5 text-gray-400 cursor-pointer hover:text-gray-300"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            onClick={copyToClipboard}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
            />
          </svg>
        </div>
      </Transition>
      <Transition
        show={copied}
        appear={true}
        enter="transition ease-out duration-150 transform"
        enterFrom="opacity-0 scale-95"
        enterTo="opacity-500 scale-100"
        leave="transition ease-in duration-200 transform"
        leaveFrom="opacity-100 scale-100"
        leaveTo="opacity-0 scale-95"
      >
        <div className="absolute top-2 right-1 mt-1 mr-2">
          <span className="select-none inline-flex items-center px-2 rounded text-xs font-medium leading-4 bg-gray-900 text-gray-400">
            Copied
          </span>
        </div>
      </Transition>
    </div>
  );
};
