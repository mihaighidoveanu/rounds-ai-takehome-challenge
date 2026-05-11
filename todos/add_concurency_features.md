Before growing the worker thread pool (now it is only 1 concurrent job), we need to make sure our shared state objects are safe from racing. 

Need an analysis on what to implement.