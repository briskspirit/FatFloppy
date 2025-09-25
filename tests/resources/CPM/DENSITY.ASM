	title	'Density Setter For Lifeboat 2D CP/M'
	length	60

*****************************************************************
*								*
* Density program to change the current density of any drive.	*
* Density allows the change of density by passing parameters	*
* on the input line, or by interogation if no parameters are	*
* passed. The parameters passed are:				*
*								*
*	1) Drive name. e.g. A,B,C,D,a,b,c,d			*
*	   Selects the designated drive for density change.	*
*	2) Density to set drive to. e.g. 0,1,S,D,s,d		*
*	   0,S,s = single density				*
*	   1,D,d = double density				*
*								*
* examples:							*
*	density b d						*
*	density b D						*
*	density b 1						*
*	density B d						*
*	density B D						*
*	density B 1						*
* all set drive "B" to double density.				*
*								*
* Other parameters are also recognized:				*
*								*
*	1) "?" will print out what the current density of each	*
*	   drive is.						*
*	2) "*" will allow you to determine the density of an	*
*	   unknown diskette.					*
*								*
* examples:							*
*	density ?						*
* will print the density of each disk drive.			*
*								*
*	density *						*
* will prompt you to insert the diskette into drive A, then	*
* print out what the density of the diskette is.		*
*								*
* Written for Thinker Toys by Bobby Dale Gifford.		*
* 6/28/79							*
*								*
*****************************************************************

revnum	equ	5		;Revision Number

	org	100h

origin	equ	0e000h		;Disk Jockey 2D address
acr	equ	0dh		;carriage return
alf	equ	0ah		;line feed
entry	equ	5		;BDOS entry point
tbuff	equ	80h		;Command line buffer
wboot	equ	0		;Warm boot location

start	lda	tbuff		;Get number of characters on command line
	ana	a		;Test for none typed
	jz	begin		;Interogate user if none typed
	lda	tbuff+2		;Get first command char
	cpi	'?'		;Question mark ?
	jz	drvden		;Take jump if true
	cpi	'*'		;Asterik ?
	jnz	sett		;Take jump if not

*****************************************************************
*								*
* This routine will determine the density of an unknown		*
* diskette. It reads in track 1 sector 1 of the disk and then	*
* calls the status routine on the Disk Jockey 2D to determine	*
* what the density of the diskette was. It then prints the	*
* appropriate message.						*
*								*
*****************************************************************

	lxi	d,insmsg	;Insert diskette in A message
	call	pbuff
	call	readcon		;Get one charactor
	mvi	c,0		;Drive to select (A)
	call	origin+1bh	;Select drive A
	call	origin+9	;Home the drive
	mvi	c,1		;Seek to track one
	call	origin+0ch	;Do the seek
	mvi	c,1		;Set the sector to read
	call	origin+0fh	;Setsec
	lxi	b,buff		;Set up DMA address
	call	origin+12h	;Setdma
	call	origin+15h	;Read in the sector
	jnc	readok		;Test for error
	lxi	d,nordmsg	;Can't read the sector
	call	pbuff		;Print error message
	jmp	exit1		;Done

*****************************************************************
*								*
* The sector was successfully read in from the disk. Now	*
* determine the density and print the correct message.		*
*								*
*****************************************************************

readok	call	origin+27h	;Disk status routine
	mov	a,d		;MSB of register D contains density
	ani	80h		;Strip off density bit
	lxi	d,sdmsg		;Single density message
	jz	sgl		;Print it if single
	lxi	d,ddmsg		;Else double density message
sgl	push	d		;Save message to print
	lxi	d,denmsg1	;Print first part of message
	call	pbuff
	pop	d		;Restore density message
	call	pbuff		;Print second part of message
exit1	lxi	d,denmsg2	;Print third part of message
	call	pbuff
	call	readcon		;Wait for drive A to be re-inserted
	jmp	exit		;Done

*****************************************************************
*								*
* This routine prints out what the density of each diskette	*
* in each drive SHOULD be. It does not print the density of	*
* each diskette in each drive.					*
*								*
*****************************************************************

drvden	lhld	wboot+1		;Get pointer to jump table
	mvi	l,0		;Round off to page boundry
	lxi	d,378h		;Offset for density table
	dad	d		;Form absolute address
	lxi	d,aeq		;Print A equals message
	call	pbuff
	call	psddd		;Print the density of A
	lxi	d,beq		;Print B equals message
	call	pbuff
	call	psddd		;Print the density of B
	lxi	d,ceq		;Print C equals message
	call	pbuff
	call	psddd		;Print the density of C
	lxi	d,deq		;Print D equals message
	call	pbuff
	call	psddd		;Print the density of D
	jmp	exit

*****************************************************************
*								*
* Routine to print the density of the drive pointed to by H&L	*
* in the density map table.					*
*								*
*****************************************************************

psddd	mov	a,m		;Get density of drive
	inx	h		;Bump to next drive
	ana	a		;Test for single or double
	lxi	d,sdmsg		;Single density message
	jz	pbuff		;Exit by printing if single
	lxi	d,ddmsg		;Else print double density message
	jmp	pbuff		;Done

*****************************************************************
*								*
* Sett determines if the parmeters passed to it are valid and	*
* then if true, sets the drive to the specified density.	*
*								*
*****************************************************************

sett	sui	'A'		;Test first parameter for A-D
	cpi	4
	jnc	exit		;Exit with no change if invalid
	adi	'A'		;Restore and save in message
	sta	drv
	lda	tbuff+4		;Get second parameter
	sui	'0'		;Test for zero or one
	cpi	2
	jc	insert		;Ok if carry
	sui	23q		;Test for D or d
	cpi	1
	jz	insert		;Ok if zero
	sui	20q		;Test for S or s
	jz	insert		;Ok if zero
	jmp	exit		;Else error, exit with no change

*****************************************************************
*								*
* Prompt message sent out if no parameters passed.		*
*								*
*****************************************************************

prompt	db	acr,alf
	db	'Density setter for Disk Jockey 2D - vers 1.'
	db	revnum+'0'
	db	acr,alf
	db	'Press "E" at any time for EXIT with NO change'
	db	acr,alf
	db	'$'

*****************************************************************
*								*
* Begin interogates the console device for which drive and	*
* what density to set it to.					*
*								*
*****************************************************************

begin	lxi	d,prompt	;Print prompt message to console
	call	pbuff
begin1	lxi	d,drvmsg	;Get drive letter to set
	call	pbuff
	call	readcon		;Get the character
	cpi	'E'		;Test for exit char
	jz	exit		;Exit if true
	sui	'A'		;Test for A-D charactor
	cpi	4
	jnc	begin1		;Ask again if invalid
	adi	'A'		;Restore and save in message
	sta	drv
badnum	lxi	d,densmsg	;Ask for density to set drive to
	call	pbuff
	call	readcon		;Get density char
	cpi	'E'		;Test for exit char
	jz	exit		;Exit if true
	sui	'0'		;Test for 0 or 1
	cpi	2
	jc	insert		;Ok if carry
	sui	23q		;Test for D or d
	cpi	1
	jz	insert		;Ok if zero
	sui	20q		;Test for S or s
	jnz	badnum		;Ask again if invalid
insert	sta	dens		;Put density in safe place
	lxi	d,waitmsg	;Wait for diskette to be inserted
	call	pbuff
	lda	dens		;Print proper density message
	ora	a
	lxi	d,sdmsg		;Single density
	jz	next
	lxi	d,ddmsg		;Double density
next	call	pbuff		;Print the density
	lxi	d,retmsg	;Rest of the message
	call	pbuff
	call	readcon		;Get response form console
	cpi	'E'		;Test for exit char
	jz	exit		;Exit with no change if true
	cpi	acr		;Test for carriage return
	jnz	begin1		;Start again if no good
	lhld	wboot+1		;Get address of CBIOS jump table
	mvi	l,0		;Round to page
	lxi	d,378h		;Offset for density table
	dad	d		;Form actual address
	lda	drv		;Get drive letter
	sui	'A'		;Strip for offset into density table
	mvi	d,0		;Set high byte to zero
	mov	e,a		;Complete offset
	dad	d		;Form actual address
	lda	dens		;Get new density
	ani	1		;Stripe to ONe or zero
	mov	m,a		;Insert new density
	lda	drv		;Get drive again
	sui	'A'		;Strip to 0-3
	mov	c,a		;Prep for select
	call	select		;select the drive
	lxi	d,donemsg	;All done message
	call	pbuff
reset	mvi	c,0dh		;Have CP/M reset everything
	jmp	entry		;Exit normally and with success

*****************************************************************
*								*
* Exit to CP/M with no change.					*
*								*
*****************************************************************

exit	lxi	d,exitmsg	;Exit with no change message
	call	pbuff
	jmp	reset		;Have CP/M reset everything

*****************************************************************
*								*
* Print buffer routine.						*
*								*
*****************************************************************

pbuff	mvi	c,9		;Print buffer command
	push	h		;Only save H&L
	call	entry		;Print the buffer
	pop	h		;Restore H&L
	ret			;All done

*****************************************************************
*								*
* Read one charactor from the console. Fold the case to upper.	*
*								*
*****************************************************************

readcon	mvi	c,1		;Read console command
	call	entry		;Get the char
	cpi	141q		;Check if less than lower case A
	rc			;Return if true
	cpi	173q		;Check of greater than lower case Z
	rnc			;Return if true
	sui	40q		;Convert the lower case char to upper
	ret			;All done

*****************************************************************
*								*
* Select calls the CBIOS select routine.			*
*								*
*****************************************************************

select	lhld	wboot+1		;Warm boot address
	lxi	d,18h		;Offset to Select routine
	dad	d		;Form actual address
	pchl			;Go there

*****************************************************************
*								*
* Messages printed by the program.				*
*								*
*****************************************************************

drvmsg	db	acr,alf
	db	'Change which drive (A,B,C, or D) ? $'
densmsg	db	acr,alf
	db	'Set single or double density '
	db	'(0 or s = SD, 1 or d = dd) ? $'
waitmsg	db	acr,alf,acr,alf
	db	'First INSERT proper density disk.'
	db	acr,alf
	db	'Then press "RETURN" to set drive '
drv	db	'  to $'
sdmsg	db	'single density.$'
ddmsg	db	'double density.$'
retmsg	db	acr,alf
	db	'(or press "E" to EXIT, "N" for NEW '
	db	'parameters) $'
donemsg	db	acr,alf,acr,alf
	db	'Density change complete - Returning to CP/M.$'
exitmsg	db	acr,alf,acr,alf
	db	'Returning to CP/M - NO change made.$'
dens	db	0
aeq	db	'Diskettes in drives must have'
	db	' these densities.'
	db	acr,alf
	db	'A = $'
beq	db	acr,alf
	db	'B = $'
ceq	db	acr,alf
	db	'C = $'
deq	db	acr,alf
	db	'D = $'
insmsg	db	acr,alf
	db	'Insert diskette into drive "A" (return) : $'
nordmsg	db	acr,alf
	db	'Can not read disk$'
denmsg1	db	acr,alf
	db	'Diskette is $'
denmsg2	db	acr,alf
	db	'Insert CP/M diskette into drive "A" (return) : $'
	ds	0ffh-($ mod 100h)
buff	equ	$+1
